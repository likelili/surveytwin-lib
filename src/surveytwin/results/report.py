#!/usr/bin/env python3
"""
Simulation Report — deterministic engine (ported from the website's simulation_report.py).

Purely computed from the QSF + the simulated-responses CSVs. NO LLM/agents.
Emits: manifest.json, report.json, report.html  (per Simulation_Report_Spec.md §2-§6).

Usage:
  build_report.py --qsf X.qsf --values responses_values.csv --labels responses_labels.csv --out OUTDIR
"""
import argparse, csv, hashlib, json, math, os, re, statistics as st

# answer_status markers. Mirror services.v2.simulation_executor (this module
# stays stdlib-only so bundles can be re-reported standalone).
#   needs_validation — legacy umbrella (pre-split bundles); treated as a skip.
#   skipped          — model gave nothing for a shown, forced question.
#   out_of_range     — model gave a non-empty answer that matched no option → blanked.
#   cascade_hidden   — hidden by display logic whose condition read a FLAGGED
#                      upstream answer; NOT a clean structural blank (C14). It was
#                      never shown, so it does NOT count as fielded — only as its
#                      own tainted bucket, carved out of what would be structural.
NEEDS_VALIDATION = "needs_validation"
SKIPPED = "skipped"
OUT_OF_RANGE = "out_of_range"
CASCADE_HIDDEN = "cascade_hidden"
_SKIP_STATUSES = frozenset({NEEDS_VALIDATION, SKIPPED})
# Fielded-flagged (twin was shown it) — cascade_hidden is deliberately NOT here.
_FLAGGED_STATUSES = frozenset({NEEDS_VALIDATION, SKIPPED, OUT_OF_RANGE})

# Report-level status is a THRESHOLD over the fraction of PROBLEMATIC questions
# (real quality/process issues), never any single flag — one odd question must
# not paint a whole study "caution". Low-variance descriptive tags (always/never
# selected, column-empty, constant) stay as per-question chips and never drive
# the report status; unsupported QSF features are informational, not a driver.
# A separate threshold on the failed-call fraction (twins whose whole call
# produced no usable answer) captures a silently-degraded batch. Tunable.
REPORT_CAUTION_Q_FRAC = 0.20
REPORT_ERROR_Q_FRAC = 0.40
REPORT_CAUTION_CALL_FRAC = 0.10
REPORT_ERROR_CALL_FRAC = 0.30
from datetime import datetime, timezone

# Default run seed used when a run didn't set one (mirrors
# persona_sampling.PERSONA_SELECTION_SEED); it drives both the sample and the
# survey randomization. Imported with a fallback so the standalone
# build_report.py CLI still works without package context.
_DEFAULT_SELECTION_SEED = 42  # the library's default run seed


def _persona_bank_label(pb, has_config):
    """Human-readable persona-bank name. On the platform, no explicit bank
    selection means the built-in bank — label it exactly as the run-settings
    UI does ("Twin-2K-500 (built-in)") instead of showing a dash whenever we
    know the run's config."""
    if isinstance(pb, dict):
        return pb.get("name") or pb.get("bank_id") or "custom twin bank"
    if pb:
        return pb
    return "Twin-2K-500 (built-in)" if has_config else None

# ---------------- QSF metadata + fidelity (parse JSON directly; no repo dep) -------------
FULL_TYPES = {"MC", "TE", "DB", "Matrix", "Slider", "Rank", "RO", "SBS"}
DETECTED_TYPES = {"Timing", "Meta", "Captcha"}
def support_status(qtype, selector):
    if qtype == "CS":                      return "Partial"     # standalone constant sum
    if qtype == "TE" and selector == "FORM": return "Partial"
    if qtype in FULL_TYPES:                return "Full"
    if qtype in DETECTED_TYPES:            return "Detected"
    return "Unsupported"

def _administered_qids(els):
    """QIDs a respondent can actually be shown: questions that live in a real
    (non-Trash) block AND whose block is referenced by the SurveyFlow.

    A .qsf keeps every question ever authored, including a "Trash / Unused
    Questions" block and blocks dropped from the flow — listing those in the
    fidelity table describes the file, not the study that was fielded. Returns
    None when the file has no block/flow structure to filter on, meaning
    "cannot tell, keep everything" (never silently drop real questions)."""
    block_qids, trash_blocks, all_blocks = {}, set(), set()
    for e in els:
        if e.get("Element") != "BL":
            continue
        payload = e.get("Payload")
        blocks = payload.values() if isinstance(payload, dict) else payload
        for b in (blocks or []):
            if not isinstance(b, dict):
                continue
            bid = str(b.get("ID") or "").strip()
            if not bid:
                continue
            all_blocks.add(bid)
            if str(b.get("Type") or "").strip().lower() == "trash":
                trash_blocks.add(bid)
            qids = {str(x.get("QuestionID") or "").strip()
                    for x in (b.get("BlockElements") or [])
                    if isinstance(x, dict) and x.get("Type") == "Question"}
            block_qids[bid] = {q for q in qids if q}
    if not block_qids:
        return None
    # Blocks the survey flow actually routes through.
    flow_blocks = set()
    for e in els:
        if e.get("Element") != "FL":
            continue
        for bid in re.findall(r'"ID"\s*:\s*"(BL_[A-Za-z0-9]+)"', json.dumps(e.get("Payload") or {})):
            flow_blocks.add(bid)
    live = (flow_blocks & set(block_qids)) - trash_blocks if flow_blocks else set(block_qids) - trash_blocks
    if not live:
        return None
    out = set()
    for bid in live:
        out |= block_qids[bid]
    return out or None


def load_qsf(path):
    d = json.load(open(path))
    els = d["SurveyElements"]
    entry = d.get("SurveyEntry", {})
    administered = _administered_qids(els)
    sq = [e["Payload"] for e in els if e["Element"] == "SQ"]
    if administered is not None:
        # Keep only what a twin can be shown; a question with no QID cannot be
        # matched to a block, so it is kept rather than silently dropped.
        sq = [p for p in sq
              if not str(p.get("QuestionID") or "").strip()
              or str(p.get("QuestionID")).strip() in administered]
    questions = []
    # Instrumentation elements a respondent never sees and never answers (page
    # timers, meta info, captcha). They are part of the file, not of the
    # questionnaire, so they do not belong in a table of what was administered
    # — the platform's question_logic.json path already omits them, and the
    # support counts of the two paths agree once they are omitted here too.
    NON_ADMINISTERED_TYPES = {"Timing", "Meta", "Captcha"}
    instrumentation = [p for p in sq
                       if str(p.get("QuestionType") or "").strip() in NON_ADMINISTERED_TYPES]
    sq = [p for p in sq
          if str(p.get("QuestionType") or "").strip() not in NON_ADMINISTERED_TYPES]
    for p in sq:
        qtype = p.get("QuestionType"); sel = p.get("Selector")
        ch = p.get("Choices") or {}; rv = p.get("RecodeValues") or {}
        ans = p.get("Answers") or {}
        allowed = None
        if qtype == "MC" and ch:
            if any("${" in str((c or {}).get("Display", "")) for c in ch.values() if isinstance(c, dict)):
                allowed = None   # piped option text — no static option set to validate against
            else:
                allowed = sorted({str(v) for v in rv.values()}) if rv else sorted(str(k) for k in ch.keys())
        elif qtype == "Matrix" and sel in ("Likert", "Bipolar", "Profile") and ans:
            # single-answer matrix: valid values are the SCALE point codes (Answers), not the row statements
            allowed = sorted(
                (str(rv.get(str(key), key)) for key in ans.keys()),
                key=lambda x: (len(x), x),
            )
        pj = json.dumps(p)
        sm = re.search(r'"CSSliderMin":\s*"?(-?\d+\.?\d*)"?', pj)
        sx = re.search(r'"CSSliderMax":\s*"?(-?\d+\.?\d*)"?', pj)
        _v = p.get("Validation")
        vs = (_v.get("Settings") or {}) if isinstance(_v, dict) else {}
        total = vs.get("ChoiceTotal") if isinstance(vs, dict) else None
        constraints = {"allowed": allowed,
                       "min": float(sm.group(1)) if sm else None,
                       "max": float(sx.group(1)) if sx else None,
                       "total": float(total) if total and str(total).replace(".", "").isdigit() else None}
        # Response requirement, straight from the instrument: only "ON" is a
        # forced response. "RequestResponse" is a soft prompt the respondent may
        # dismiss, and "OFF"/absent is optional — so a blank there is a
        # legitimate non-response, not a fidelity failure. Reading this from the
        # QSF means forced-ness no longer depends on question_logic.json being
        # present (without it every blank was conservatively called forced).
        _force = str(vs.get("ForceResponse") or "").strip()
        # Declared numeric validation (Type=ContentType + ContentType=ValidNumber)
        # takes precedence over inference from the observed answers.
        _declared_numeric = (str(vs.get("Type") or "").strip() == "ContentType"
                             and str(vs.get("ContentType") or "").strip() == "ValidNumber")
        questions.append({
            "qid": p.get("QuestionID") or p.get("DataExportTag"),
            "tag": p.get("DataExportTag"),
            "type": qtype, "selector": sel,
            "text": re.sub(r"<[^>]+>", "", p.get("QuestionText") or "").replace("&nbsp;", " ").strip()[:140],
            "support": support_status(qtype, sel), "constraints": constraints,
            "forced": _force == "ON", "forced_known": bool(_force),
            "declared_numeric": _declared_numeric,
        })
    # logic features + warnings, scanned from raw payloads/flow
    blob = json.dumps(els)
    feat = {
        "DisplayLogic": '"DisplayLogic"' in blob, "SkipLogic": '"SkipLogic"' in blob,
        "Branch": '"Type":"Branch"' in blob.replace(" ", ""), "Loop&Merge": "LoopingOptions" in blob,
        "Piping": "${" in blob, "Randomization": '"Randomization"' in blob,
        "EmbeddedData": "EmbeddedData" in blob,
    }
    warnings = []
    if instrumentation:
        _kinds = sorted({str(p.get("QuestionType") or "").strip() for p in instrumentation})
        warnings.append(
            f"{len(instrumentation)} survey-metadata element(s) detected ({', '.join(_kinds)}) — "
            "not shown to twins and not simulated; excluded from the question list below.")
    if '"QuestionJS"' in blob: warnings.append("Question JavaScript present — not executed during simulation.")
    if '"WebService"' in blob or "WebService" in json.dumps([e.get("Element") for e in els]):
        warnings.append("Web Service flow element present — not called during simulation.")
    for q in questions:
        if q["support"] == "Unsupported":
            warnings.append(f"Unsupported question type '{q['type']}' ({q['tag']}).")
        elif q["support"] == "Partial":
            warnings.append(f"Partial support: {q['type']}/{q['selector']} ({q['tag']}).")
    # randomization summary (flow randomizers + per-question choice randomization)
    flblob = json.dumps([e for e in els if e["Element"] == "FL"]).replace(" ", "")
    rand_flow = '"BlockRandomizer"' in flblob or '"Type":"Randomizer"' in flblob
    rand_even = '"evenpresentation":true' in flblob.lower()
    nrq = blob.count('"Randomization"')
    from collections import Counter
    return {
        "title": entry.get("SurveyName"),
        "n_questions": len(questions),
        "type_counts": dict(Counter(q["type"] for q in questions)),
        "questions": questions, "features": feat, "warnings": warnings,
        "rand_flow": rand_flow, "rand_even": rand_even, "rand_choice_count": nrq,
        "n_full": sum(q["support"] == "Full" for q in questions),
        "n_partial": sum(q["support"] == "Partial" for q in questions),
        "n_unsupported": sum(q["support"] == "Unsupported" for q in questions),
    }

# ---------------- responses CSV (Qualtrics 3-header format) -------------
def load_responses(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    names, texts, imports = rows[0], rows[1], rows[2]
    qids = []
    for c in imports:
        m = re.search(r'"ImportId":"([^"]+)"', c or "")
        qids.append(m.group(1) if m else "")
    data = rows[3:]
    cols = []
    for j, name in enumerate(names):
        cols.append({"name": name, "text": (texts[j] if j < len(texts) else ""),
                     "qid": (qids[j] if j < len(qids) else ""),
                     "values": [(r[j] if j < len(r) else "") for r in data]})
    return cols, len(data)

def is_num(x):
    try: float(x); return True
    except: return False


# One COMPLETE scalar number and nothing else. Deliberately stricter than
# is_num: a survey answer like "0-5" (a typed range), "22 years" (a unit),
# "20-30", "1,000" (separator) or "nan"/"inf" is NOT a scalar measurement, so a
# text-entry column containing one must stay text and be preserved verbatim —
# never split, coerced, or partially parsed.
_SCALAR_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def is_scalar_number(x):
    text = str(x).strip()
    if not text or not _SCALAR_NUMBER_RE.match(text):
        return False
    try:
        return math.isfinite(float(text))
    except (TypeError, ValueError):
        return False


def all_scalar_numbers(values):
    """True when EVERY nonblank value is one complete scalar number."""
    nonblank = [v for v in values if str(v).strip() != ""]
    return bool(nonblank) and all(is_scalar_number(v) for v in nonblank)

# Abbreviation -> full name (shown in the report instead of codes like DB/TB)
QTYPE_NAMES = {"MC": "Multiple Choice", "TE": "Text Entry", "DB": "Text / Graphic", "Matrix": "Matrix",
               "Slider": "Slider", "Rank": "Rank Order", "RO": "Rank Order", "SBS": "Side-by-Side",
               "CS": "Constant Sum", "Timing": "Timing", "Meta": "Meta Info", "Captcha": "Captcha"}
SELECTOR_NAMES = {"SAVR": "Single Answer (Vertical)", "SAHR": "Single Answer (Horizontal)",
                  "SACOL": "Single Answer (Column)", "DL": "Dropdown List", "SB": "Select Box",
                  "MAVR": "Multi Answer (Vertical)", "MAHR": "Multi Answer (Horizontal)",
                  "MACOL": "Multi Answer (Column)", "MSB": "Multi-Select Box", "SL": "Single Line",
                  "ML": "Multi Line", "ESTB": "Essay Text Box", "PW": "Password", "FORM": "Form Field",
                  "TB": "Text Block", "GRB": "Graphic Block", "Likert": "Likert", "Bipolar": "Bipolar",
                  "Profile": "Profile", "TE": "Text-Entry Rows", "CS": "Constant Sum",
                  "HSLIDER": "Horizontal Slider", "HBAR": "Bar", "STAR": "Star Rating", "VRTL": "Vertical",
                  "DND": "Drag &amp; Drop", "SBSMatrix": "Side-by-Side Matrix", "PageTimer": "Page Timer"}
def type_label(t, sel):
    tn = QTYPE_NAMES.get(t, t or "")
    sn = SELECTOR_NAMES.get(sel, sel) if sel else ""
    return f"{tn} — {sn}" if sn else tn

def file_desc(fn):
    f = fn.lower()
    if f.endswith(".qsf"): return "Qualtrics survey file (source instrument)"
    if "values" in f: return "Response file — answers coded as values"
    if "labels" in f: return "Response file — answers as text labels"
    return "data file"

def _sub_label(text):
    """The option/row/item label of an expanded column (multi-answer MC, matrix, rank, SBS,
    slider statement, form field). Returns None for a single-column question."""
    if not text:
        return None
    # matrix multi-answer cell: "... - <row> Option: <opt>. Should this option be selected?"
    m2 = re.search(r"-\s*(.+?)\s*Option:\s*(.+?)\.?\s*Should this option be selected", text)
    if m2:
        return f"{m2.group(1).strip()} · {m2.group(2).strip()}"
    m = re.search(r"Option:\s*(.+?)\.?\s*Should this option be selected", text)
    if m:
        return m.group(1).strip()
    if " - " in text:
        tail = text.split(" - ", 1)[1].strip()
        tail = re.sub(r"^Rank for\s+", "", tail)
        return tail
    return None

def _numeric_bins(nums, max_bins=10):
    """Equal-width histogram bins for a numeric column. Small integer ranges get one bin
    per integer (a 1–7 scale shows 7 bars); anything else gets up to `max_bins` bins.
    Returns [] when there is nothing to bin (constant / degenerate columns)."""
    lo, hi = min(nums), max(nums)
    if hi <= lo or len(nums) < 2:
        return []
    if all(float(v).is_integer() for v in nums) and (hi - lo) <= 12:
        start = int(lo)
        step = 1.0
        edge0 = start - 0.5
        labels = [str(v) for v in range(start, int(hi) + 1)]
    else:
        step = (hi - lo) / max_bins
        edge0 = lo
        labels = [f"{round(lo + i*step, 2):g}–{round(lo + (i+1)*step, 2):g}" for i in range(max_bins)]
    counts = [0] * len(labels)
    for v in nums:
        counts[min(len(counts) - 1, max(0, int((v - edge0) / step)))] += 1
    return [{"label": labels[i], "count": counts[i], "pct": round(100*counts[i]/len(nums), 1)}
            for i in range(len(labels))]


def _text_len_stats(nonblank):
    lens = [len(str(v).strip()) for v in nonblank]
    if not lens:
        return {}
    return {"mean_len": round(st.mean(lens), 1), "sd_len": round(st.pstdev(lens), 1) if len(lens) > 1 else 0.0,
            "min_len": min(lens), "median_len": round(st.median(lens), 1), "max_len": max(lens)}

def _skip_fields(out, values, statuses):
    """Split blanks into flagged skips (answer_status='needs_validation' — the
    twin was asked and did not usably answer) vs unflagged blanks (structural:
    not shown / not selected / optional skip-by-choice — or, conservatively,
    any blank the pipeline did not positively flag). Distribution stats are
    already valid-base (non-blank); these STRUCTURED fields make the
    by-variable behavior explicit. Keyed on the status flag, never on
    blankness. The counts live on the variable's `behavior`; no flag is
    appended — every skip surface (status, §3 table, card chip, pct-clean)
    reads these fields, so nothing depends on a flag message surviving the
    structural-blank strippers."""
    n = len(values)
    n_skipped = sum(1 for s in statuses if str(s).strip() in _SKIP_STATUSES)
    # out_of_range cells are ALSO blank (the executor blanks a non-mapping
    # answer and marks it), so they sit inside n_missing alongside skips and
    # structural blanks — count and subtract them as their own bucket.
    n_out_of_range = sum(1 for s in statuses if str(s).strip() == OUT_OF_RANGE)
    # cascade_hidden cells are blank + marked; carve them out of structural too
    # so a display-logic hide caused by an upstream skip is not miscounted as a
    # clean not-shown blank (C14).
    n_cascade = sum(1 for s in statuses if str(s).strip() == CASCADE_HIDDEN)
    out["n_valid"] = n - out["n_missing"]
    out["n_skipped"] = n_skipped
    out["n_out_of_range"] = n_out_of_range
    out["n_cascade"] = n_cascade
    # Every unflagged blank. NOT asserted to be truly "structural" — it is
    # mostly not-shown / not-selected, but may include an optional refusal or
    # unflagged loss, so the report labels it "unflagged blank", not
    # "structural" (see render).
    out["n_structural"] = max(0, out["n_missing"] - n_skipped - n_out_of_range - n_cascade)
    out["skipped_pct"] = round(100*n_skipped/n, 1) if n else 0.0


def col_stats(values, force_cat=False, force_text=False, statuses=None):
    nonblank = [v for v in values if str(v).strip() != ""]
    n, miss = len(values), len(values) - len([v for v in values if str(v).strip() != ""])
    out = {"n": n, "n_missing": miss, "missing_pct": round(100*miss/n, 1) if n else 0.0}
    if statuses is not None:
        _skip_fields(out, values, statuses)
    nbset = {str(v).strip() for v in nonblank}
    if force_text:
        out["kind"] = "text"; out.update(_text_len_stats(nonblank))
        flags = []
        if out["missing_pct"] > 5: flags.append(("caution", f"{out['missing_pct']}% missing"))
        if not nonblank: flags.append(("caution", "column entirely empty"))
        out["flags"] = flags
        return out
    # binary selection indicator (multi-answer option expansion): 1 / blank — blank = NOT selected, not missing
    if nonblank and nbset <= {"0", "1"} and "1" in nbset:
        sel = sum(1 for v in values if str(v).strip() == "1")
        out.update({"kind": "selection", "n_selected": sel, "n_total": n,
                    "select_rate": round(100*sel/n, 1) if n else 0.0, "n_missing": 0, "missing_pct": 0.0})
        flags = []
        if sel == 0: flags.append(("caution", "never selected"))
        elif sel == n: flags.append(("caution", "always selected"))
        out["flags"] = flags
        return out
    if not force_cat and nonblank and all(is_num(v) for v in nonblank):
        nums = [float(v) for v in nonblank]
        out["kind"] = "numeric"
        out["mean"] = round(st.mean(nums), 3); out["sd"] = round(st.pstdev(nums), 3) if len(nums) > 1 else 0.0
        out["min"] = min(nums); out["max"] = max(nums)
        qs = sorted(nums)
        out["q25"] = round(qs[int(.25*(len(qs)-1))], 3); out["median"] = round(st.median(nums), 3)
        out["q75"] = round(qs[int(.75*(len(qs)-1))], 3)
        # binary (yes/no expansion) selection rate
        if set(nums) <= {0.0, 1.0}: out["select_rate"] = round(100*sum(nums)/len(nums), 1)
        bins = _numeric_bins(nums)
        if bins: out["bins"] = bins
    else:
        from collections import Counter
        c = Counter(nonblank)
        out["kind"] = "categorical"; out["distinct"] = len(c)
        out["top"] = [{"label": k, "count": v, "pct": round(100*v/len(nonblank), 1)} for k, v in c.most_common(12)]
        # open text only if responses are mostly unique AND long (not just long categorical labels)
        distinct_ratio = len(c) / len(nonblank) if nonblank else 0
        if nonblank and distinct_ratio > 0.7 and st.mean([len(str(v)) for v in nonblank]) > 25:
            out["kind"] = "text"; out.update(_text_len_stats(nonblank))
    # flags
    flags = []
    if out["missing_pct"] > 5: flags.append(("caution", f"{out['missing_pct']}% missing"))
    if out["kind"] != "text" and len(set(nonblank)) == 1 and nonblank: flags.append(("caution", "constant (no variation)"))
    if not nonblank: flags.append(("caution", "column entirely empty"))
    out["flags"] = flags
    return out

def sha256(path):
    h = hashlib.sha256(); h.update(open(path, "rb").read()); return h.hexdigest()

# ---------------- bundle (no QSF): derive structure from per-twin question files -------
def base_qid(imp):
    # find the QID anywhere (handles loop prefixes like "1_QID43" and SBS "QID35#1_1")
    m = re.search(r"(QID\d+)", imp or ""); return m.group(1) if m else (imp or "")

def load_questions_dir(qdir):
    from collections import Counter
    qmap = {}; tc = Counter()
    files = [f for f in os.listdir(qdir) if f.endswith(".txt")]
    for fn in files[:400]:
        txt = open(os.path.join(qdir, fn), encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r"\[(QID[^\]]+)\]\s*(.*?)\n\s*Type:\s*(\w+)", txt, re.S):
            qid = m.group(1).strip()
            if qid not in qmap:
                qmap[qid] = {"text": re.sub(r"\s+", " ", m.group(2)).strip()[:140], "type": m.group(3).strip()}
    for q in qmap.values(): tc[q["type"]] += 1
    return qmap, dict(tc), len(files)

# ---------------- question_logic.json: block order + per-question settings/logic -------------
def _settings_desc(q):
    s = q.get("Settings") or {}; bits = []
    fr = s.get("ForceResponse")
    if fr and fr != "OFF":
        bits.append("force response: " + ("requested" if fr == "RequestResponse" else "required"))
    vt = s.get("ValidationType")
    if vt and vt != "None":
        rules = [f"{k}={s[k]}" for k in ("MinChars", "TotalChars", "MinChoices", "MaxChoices", "ChoiceTotal") if s.get(k)]
        bits.append("validation " + vt + (" (" + ", ".join(rules) + ")" if rules else ""))
    rng = q.get("Range")
    if isinstance(rng, dict) and (rng.get("Min") is not None or rng.get("Max") is not None):
        bits.append(f"range {rng.get('Min')}–{rng.get('Max')}")
    if q.get("DefaultChoices"):
        bits.append("has default")
    return " · ".join(bits)

def _logic_desc(q, branch_cond=None, randomized=False):
    bits = []
    if branch_cond: bits.append(branch_cond)
    dl = q.get("DisplayLogic")
    if isinstance(dl, dict):
        try:
            e = dl["0"]["0"]
            bits.append(f"shown if {e.get('LeftOperand')} {str(e.get('Operator','')).lower()} {e.get('RightOperand','')}".strip())
        except Exception:
            bits.append("has display logic")
    sl = q.get("SkipLogic")
    if isinstance(sl, list) and sl:
        parts = []
        for r in sl:
            m = re.search(r"/(\d+)$", r.get("ChoiceLocator", ""))
            parts.append(f"choice {m.group(1) if m else '?'} → {r.get('SkipToDestination')}")
        bits.append("skip logic: " + "; ".join(parts))
    rnd = q.get("Randomization")
    if isinstance(rnd, dict) and rnd.get("Type"):
        bits.append("choice randomization: " + rnd["Type"])
    if randomized: bits.append("block randomized")
    return " · ".join(bits)

def _branch_cond_desc(bl):
    try:
        d = bl["0"]["0"].get("Description", "")
        return "shown only when " + re.sub(r"<[^>]+>", "", d).replace("If", "", 1).strip()
    except Exception:
        return "shown only on a branch condition"

def _int_or_none(v):
    try: return int(str(v).strip())
    except Exception: return None

def excluded_blocks_of(cfg):
    """[(block ID, name or None)] for the blocks a run excluded, from its config's
    ``qsf_block_exclusions``, in any of the shapes runs write:
    {QSF attachment or survey: [block IDs]} (the website's form, the library),
    {block name: "ID1,ID2"} (the validation-bundle driver: one name can match
    two blocks), or {block name: block ID} (older library runs)."""
    raw = cfg.get("qsf_block_exclusions") if isinstance(cfg, dict) else None
    out, seen = [], set()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, list):
                ids, name = value, None
            else:
                ids, name = str(value or "").split(","), (str(key).strip() or None)
            for bid in ids:
                bid = str(bid or "").strip()
                if bid and bid not in seen:
                    seen.add(bid)
                    out.append((bid, name))
    return out

def excluded_block_ids_of(cfg):
    """The block IDs a run excluded (see ``excluded_blocks_of``)."""
    return [bid for bid, _name in excluded_blocks_of(cfg)]

def load_question_logic(path):
    d = json.load(open(path))
    blocks, qmeta, order = [], {}, [0]
    rand_groups, branch_blocks = [], {}   # arm-block names per flow randomizer; block -> branch condition
    embedded_fields = {}      # field -> sorted set of values it is set to anywhere in the flow
    assignment_fields = {}    # field set under a Randomizer -> {n_arms, branch_cond, branch_logic}
    branch_logic_raw = {}     # block name -> raw enclosing BranchLogic payload (for exposure eval)
    display_logic_raw = {}    # qid -> raw DisplayLogic payload
    def walk(elements, branch_cond=None, randomized=False, group=None, branch_raw=None, in_rand=False):
        for el in elements:
            et = el.get("ElementType")
            if et == "Block":
                looping = (el.get("Options") or {}).get("Looping")
                bname = el.get("BlockName")
                if group is not None: group.append(bname)
                if branch_cond: branch_blocks[bname] = branch_cond
                if branch_raw is not None: branch_logic_raw[bname] = branch_raw
                qs = []
                for q in el.get("Questions", []):
                    order[0] += 1
                    qid = q.get("QuestionID")
                    if isinstance(q.get("DisplayLogic"), dict):
                        display_logic_raw[qid] = q["DisplayLogic"]
                    sd, ld = _settings_desc(q), _logic_desc(q, branch_cond, randomized)
                    if looping: ld = (ld + " · " if ld else "") + "loop & merge"
                    qst = q.get("Settings") or {}
                    qmeta[qid] = {"block": bname, "block_id": el.get("BlockID"), "order": order[0], "settings": sd, "logic": ld,
                                  "qtype": q.get("QuestionType"),
                                  # single-answer matrices carry their scale in Columns/ColumnsID —
                                  # same role as an MC's Options/OptionsID (labels + export codes)
                                  "options": (q.get("Options") if isinstance(q.get("Options"), list) else None)
                                             or (q.get("Columns") if isinstance(q.get("Columns"), list) else None),
                                  "option_ids": (q.get("OptionsID") if isinstance(q.get("OptionsID"), list) else None)
                                                or (q.get("ColumnsID") if isinstance(q.get("ColumnsID"), list) else None),
                                  "recode_values": (q.get("RecodeValues")
                                                    if isinstance(q.get("RecodeValues"), dict)
                                                    else {}),
                                  "min_chars": _int_or_none(qst.get("MinChars")),
                                  "content_type": (qst.get("ContentType") or "").strip() or None,
                                  "forced": qst.get("ForceResponse") == "ON"}
                    qs.append(qid)
                blocks.append({"name": bname, "id": el.get("BlockID"), "questions": qs, "randomized": randomized})
            elif et == "Randomizer":
                g = {"blocks": [], "select": _int_or_none(el.get("SelectElements"))}
                rand_groups.append(g)
                # embedded-data assignment pattern: sibling EmbeddedData children each
                # setting the same field to a different value = randomized assignment
                arm_fields = {}
                for child in el.get("Elements", []):
                    if child.get("ElementType") == "EmbeddedData":
                        for row in child.get("EmbeddedData", []) or []:
                            f = str(row.get("Field", "") or "").strip()
                            if f:
                                arm_fields.setdefault(f, set()).add(str(row.get("Value", "")))
                for f, vals in arm_fields.items():
                    prev = assignment_fields.get(f, {"n_arms": 0})
                    assignment_fields[f] = {"n_arms": max(prev.get("n_arms", 0), len(vals)),
                                            "branch_cond": branch_cond, "branch_logic": branch_raw}
                walk(el.get("Elements", []), branch_cond, True, g["blocks"], branch_raw, True)
            elif et == "Branch":
                walk(el.get("Elements", []),
                     _branch_cond_desc(el.get("BranchLogic", {})), randomized, group,
                     el.get("BranchLogic") if isinstance(el.get("BranchLogic"), dict) else branch_raw, in_rand)
            elif et == "Group":
                # flow groups are pure containers; recurse so group-nested blocks
                # (Randomizer -> Group -> Randomizer -> Block) keep their mapping
                walk(el.get("Elements", []), branch_cond, randomized, group, branch_raw, in_rand)
            elif et == "EmbeddedData":
                for row in el.get("EmbeddedData", []) or []:
                    f = str(row.get("Field", "") or "").strip()
                    if f:
                        embedded_fields.setdefault(f, set()).add(str(row.get("Value", "")))
    walk(d.get("Elements", []))
    return {"blocks": blocks, "qmeta": qmeta, "block_names": [b["name"] for b in blocks],
            "rand_groups": [g for g in rand_groups if g["blocks"]], "branch_blocks": branch_blocks,
            "embedded_fields": {f: sorted(v) for f, v in embedded_fields.items()},
            "assignment_fields": assignment_fields,
            "branch_logic_raw": branch_logic_raw, "display_logic_raw": display_logic_raw}

_QLOC_RE = re.compile(r"q://(QID\d+)/SelectableChoice/(\d+)")

def eval_condition(cond, get_field, get_answer, get_choice_tokens=None):
    """Evaluate one Qualtrics logic condition for one respondent.
    get_field(name) -> exported embedded-data value (str) or None.
    get_answer(qid) -> the respondent's exported answer value/label strings (set) or None.
    Returns True / False / None (undecidable)."""
    lt = str(cond.get("LogicType", "") or "").strip()
    op = str(cond.get("Operator", "") or "").strip()
    left = str(cond.get("LeftOperand", "") or "").strip()
    right = str(cond.get("RightOperand", "") or "").strip()
    if lt == "BooleanValue":   # "If False" / "If True": the same for every respondent
        v = cond.get("Value")
        return v if isinstance(v, bool) else str(v or "").strip().lower() == "true"
    if lt == "EmbeddedField":
        v = get_field(left)
        if v is None: return None
        v = str(v).strip()
        if op == "EqualTo": return v == right
        if op == "NotEqualTo": return v != right
        if op == "Contains": return right in v
        if op in ("GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual"):
            try:
                a, b = float(v), float(right)
            except ValueError:
                return None
            return {"GreaterThan": a > b, "GreaterThanOrEqual": a >= b,
                    "LessThan": a < b, "LessThanOrEqual": a <= b}[op]
        return None
    if lt == "Question":
        m = _QLOC_RE.search(left)
        if not m: return None
        qid, choice = m.group(1), m.group(2)
        tokens = get_answer(qid)
        if tokens is None: return None
        expected = (
            get_choice_tokens(qid, choice)
            if callable(get_choice_tokens)
            else {choice}
        )
        hit = bool(tokens & set(expected or {choice}))
        if op == "Selected": return hit
        if op == "NotSelected": return not hit
        return None
    return None

def logic_always_false(payload):
    """True when a display/branch logic payload is made of literal true/false
    conditions only and comes out false ("If False"): what it gates is shown
    to nobody, so its columns are the author's switched-off questions, not
    missing data."""
    leaves = []

    def collect(node):
        if isinstance(node, dict):
            if "LogicType" in node:
                leaves.append(node)
                return
            for v in node.values():
                collect(v)

    collect(payload)
    # conditions a twin cannot meet (device, location, quota...) are left out:
    # "Device is mobile Or False" is decided by its "False"
    leaves = [c for c in leaves if str(c.get("LogicType") or "").strip() in ("Question", "EmbeddedField", "BooleanValue")]
    if not leaves or any(str(c.get("LogicType") or "").strip() != "BooleanValue" for c in leaves):
        return False
    return eval_logic(payload, lambda _n: None, lambda _q: None) is False


def eval_logic(payload, get_field, get_answer, get_choice_tokens=None):
    """Evaluate a Qualtrics BranchLogic/DisplayLogic payload.

    Numbered condition GROUPS are combined by each group's `Type`
    (`AndIf`->And, `OrIf`/`ElseIf`->Or, `If`/first-> seed); conditions WITHIN a
    group are combined by each condition's `Conjuction`. Both folds run
    left-to-right (Qualtrics has no And-over-Or precedence). Undecidable leaves
    (None) are skipped; a wholly-undecidable result is None (treated as shown).

    Prior impl flattened every condition of every group into ONE list under a
    single trailing conjunction, so an AndIf across two OR-groups collapsed to
    `any(all conditions)` — a twin matching only the first group was wrongly
    exposed, inflating that question's n and missing%.
    """
    if not isinstance(payload, dict) or not payload:
        return None

    def numbered(obj):
        return [obj[k] for k in sorted((k for k in obj if str(k).isdigit()), key=int)
                if isinstance(obj[k], dict)]

    def fold(items):
        # items: (value, join_with_previous); left-to-right, undecidable dropped
        acc, started = None, False
        for val, join in items:
            if val is None:
                continue
            if not started:
                acc, started = val, True
            elif join == "Or":
                acc = acc or val
            else:
                acc = acc and val
        return acc if started else None

    def cond_join(node):
        return "Or" if str(node.get("Conjuction") or node.get("Conjunction") or "").strip() == "Or" else "And"

    def group_join(group):
        return "Or" if str(group.get("Type") or "").strip() in ("OrIf", "ElseIf") else "And"

    groups = numbered(payload)
    if not groups:  # payload is itself a single bare condition
        return (
            eval_condition(payload, get_field, get_answer, get_choice_tokens)
            if "LogicType" in payload
            else None
        )

    group_vals = []
    for gi, group in enumerate(groups):
        conds = numbered(group)
        if conds:
            gv = fold(
                [
                    (
                        eval_condition(c, get_field, get_answer, get_choice_tokens),
                        cond_join(c),
                    )
                    for c in conds
                ]
            )
        elif "LogicType" in group:
            gv = eval_condition(group, get_field, get_answer, get_choice_tokens)
        else:
            gv = None
        group_vals.append((gv, "And" if gi == 0 else group_join(group)))
    return fold(group_vals)
def qsf_struct_from_question_logic(d):
    """Build the same structure as load_qsf() from question_logic.json (no .qsf needed)."""
    from collections import Counter
    questions = []
    flags = {"rand_flow": False, "rand_even": False, "rand_choice": 0}
    feat = {"DisplayLogic": False, "SkipLogic": False, "Branch": False, "Loop&Merge": False,
            "Piping": False, "Randomization": False, "EmbeddedData": False}

    def constraints_for(q):
        qt = q.get("QuestionType"); st = q.get("Settings") or {}; sel = st.get("Selector")
        opts = q.get("Options") or []; oid = q.get("OptionsID") or []
        recodes = q.get("RecodeValues") if isinstance(q.get("RecodeValues"), dict) else {}
        cols = q.get("Columns") or []
        rng = q.get("Range") if isinstance(q.get("Range"), dict) else {}
        allowed = None
        if qt == "MC" and opts:
            if any("${" in str(o) for o in opts):
                # piped option text (e.g. ${e://Field/...}) is resolved per twin at
                # runtime — there is no static option set to validate against
                allowed = None
            else:
                raw_ids = oid if oid else [str(i) for i in range(1, len(opts) + 1)]
                allowed = [str(recodes.get(str(raw_id), raw_id)) for raw_id in raw_ids]
        elif qt == "Matrix" and sel in ("Likert", "Bipolar", "Profile") and cols:
            # values CSVs carry the authored answer codes (ColumnsID) — surveys whose
            # scale points were re-added in the editor export e.g. 7/8/9/4/5 for a
            # 1..5 display scale, so positional 1..N would false-flag every answer
            colsid = [str(x) for x in (q.get("ColumnsID") or []) if str(x).strip()]
            raw_ids = colsid if colsid else [str(i) for i in range(1, len(cols) + 1)]
            allowed = [str(recodes.get(str(raw_id), raw_id)) for raw_id in raw_ids]
        total = st.get("ChoiceTotal")
        # only range-check continuous sliders; STAR / constant-sum use other scales
        mn = mx = None
        if qt == "Slider" and sel in ("HSLIDER", "HBAR"):
            mn, mx = rng.get("Min"), rng.get("Max")
        return {"allowed": allowed, "min": mn, "max": mx,
                "total": float(total) if total and str(total).replace(".", "").isdigit() else None}

    def walk(elements):
        for el in elements:
            et = el.get("ElementType")
            if et == "Block":
                if (el.get("Options") or {}).get("Looping"):
                    feat["Loop&Merge"] = True
                for q in el.get("Questions", []):
                    st = q.get("Settings") or {}; qt = q.get("QuestionType"); sel = st.get("Selector")
                    if q.get("DisplayLogic"): feat["DisplayLogic"] = True
                    if q.get("SkipLogic"): feat["SkipLogic"] = True
                    if q.get("Randomization"): feat["Randomization"] = True; flags["rand_choice"] += 1
                    if "${" in (q.get("QuestionText") or ""): feat["Piping"] = True
                    questions.append({
                        "qid": q.get("QuestionID"), "tag": q.get("QuestionName"),
                        "type": qt, "selector": sel,
                        "text": re.sub(r"<[^>]+>", "", q.get("QuestionText") or "").replace("&nbsp;", " ").strip()[:140],
                        "support": support_status(qt, sel), "constraints": constraints_for(q)})
            elif et == "Randomizer":
                flags["rand_flow"] = True
                if el.get("EvenPresentation") is True:
                    flags["rand_even"] = True
                walk(el.get("Elements", []))
            elif et == "Branch":
                feat["Branch"] = True
                walk(el.get("Elements", []))
            elif et == "Group":
                walk(el.get("Elements", []))
            elif et == "EmbeddedData":
                feat["EmbeddedData"] = True
    walk(d.get("Elements", []))
    warnings = []
    for q in questions:
        if q["support"] == "Unsupported":
            warnings.append(f"Unsupported question type '{q['type']}' ({q['tag']}).")
        elif q["support"] == "Partial":
            warnings.append(f"Partial support: {q['type']}/{q['selector']} ({q['tag']}).")
    return {"title": (d.get("Metadata") or {}).get("SurveyName"),
            "n_questions": len(questions),
            "type_counts": dict(Counter(q["type"] for q in questions)),
            "questions": questions, "features": feat, "warnings": warnings,
            "rand_flow": flags["rand_flow"], "rand_even": flags["rand_even"], "rand_choice_count": flags["rand_choice"],
            "n_full": sum(q["support"] == "Full" for q in questions),
            "n_partial": sum(q["support"] == "Partial" for q in questions),
            "n_unsupported": sum(q["support"] == "Unsupported" for q in questions)}

# ---------------- assemble -------------
def _skip_counts(v):
    """Whether a flagged skip on this variable counts toward the skipping rate /
    study status (round-4 §10 / §8.6). A skip counts when the question is FORCED,
    or when its forced-ness is UNKNOWN (no question_logic metadata for it) — we
    never silently drop a real skip. A blank on a KNOWN-optional (ForceResponse
    off) question is a legitimate non-response and does NOT count."""
    return bool(v.get("forced")) or not bool(v.get("forced_known"))


def _default_system_prompt():
    """The standard simulation system prompt, used when the bundle didn't record
    one (older exports). Guarded import so this module stays importable for
    standalone re-reporting even when the executor isn't on the path. A CT run
    appends a bank-specific instruction, not reflected here."""
    try:
        from ..answers.executor import SIMULATION_SYSTEM_PROMPT
        return SIMULATION_SYSTEM_PROMPT
    except Exception:
        return None


def _status_cols_from_long(long_path, vcols, n_rows):
    """Reconstruct per-cell status columns (parallel to vcols) from long.csv's
    answer_status / answer_repaired — so the report needs no separate
    responses_status.csv (no longer exported). A repaired cell reads 'repaired'
    (a valid answer, not flagged); otherwise the raw answer_status. Returns None
    if long.csv is missing or lacks a TWIN_ID column to key on."""
    if not long_path or not os.path.exists(long_path):
        return None
    twin_col = next((c for c in vcols if c["name"] == "TWIN_ID"), None)
    if twin_col is None:
        return None
    eff = {}
    try:
        with open(long_path, newline="") as f:
            for row in csv.DictReader(f):
                pid = (row.get("persona_id") or "").strip()
                qid = (row.get("question_id") or "").strip()
                if not pid or not qid:
                    continue
                rep = (row.get("answer_repaired") or "").strip()
                eff[(pid, qid)] = "repaired" if rep else (row.get("answer_status") or "").strip()
    except Exception:
        return None
    if not eff:
        return None
    twin_ids = twin_col["values"]
    return [{"name": vc["name"], "qid": vc["qid"],
             "values": [eff.get((str(twin_ids[r]), vc["qid"]), "") for r in range(n_rows)]}
            for vc in vcols]


def build(values_path, labels_path, out_dir, qsf_path=None, questions_dir=None, manifest_path=None,
          question_logic_path=None, run_id="prototype-run", emit_manifest=True):
    os.makedirs(out_dir, exist_ok=True)
    vcols, n_rows = load_responses(values_path)
    lcols, _ = load_responses(labels_path) if labels_path else (vcols, n_rows)
    # Per-cell answer status (responses_status.csv, column-parallel to values/
    # labels): 'skipped'/'needs_validation' mark a flagged skip, 'out_of_range'
    # a non-mapping answer (both blanked); empty = real answer or structural
    # blank. Optional — old bundles simply lack the by-variable behavior split.
    scols = None
    _status_path = os.path.join(os.path.dirname(os.path.abspath(values_path)), "responses_status.csv")
    if os.path.exists(_status_path):
        try:
            scols, s_rows = load_responses(_status_path)
            if len(scols) != len(vcols) or s_rows != n_rows:
                scols = None   # misaligned bundle: never guess a cell's status
        except Exception:
            scols = None
    if scols is None:
        # No wide responses_status.csv (it's no longer exported) — reconstruct
        # per-cell status from long.csv, the single source going forward.
        scols = _status_cols_from_long(
            os.path.join(os.path.dirname(os.path.abspath(values_path)), "long.csv"),
            vcols, n_rows)
    qsf = load_qsf(qsf_path) if qsf_path else None
    if qsf is None:
        _qlp = question_logic_path or os.path.join(os.path.dirname(os.path.abspath(values_path)), "question_logic.json")
        if os.path.exists(_qlp):
            try:
                qsf = qsf_struct_from_question_logic(json.load(open(_qlp)))
            except Exception:
                qsf = None
    qmap, qtc, _ = load_questions_dir(questions_dir) if questions_dir else ({}, {}, 0)
    qsf_tag_map = {q["tag"]: q for q in (qsf["questions"] if qsf else [])}
    qsf_qid_map = {q["qid"]: q for q in (qsf["questions"] if qsf else [])}
    # question_logic.json: authoritative block order + per-question settings/logic
    bdir = os.path.dirname(os.path.abspath(values_path))
    qlpath = question_logic_path or os.path.join(bdir, "question_logic.json")
    qlogic = load_question_logic(qlpath) if os.path.exists(qlpath) else {
        "blocks": [], "qmeta": {}, "block_names": [], "rand_groups": [], "branch_blocks": {}}
    ql_qmeta = qlogic["qmeta"]
    block_order = {name: i for i, name in enumerate(qlogic["block_names"])}

    META_COLS = {"TWIN_ID", "demo_evidence_fixture"}
    is_demo = lambda c: c["name"].startswith("demo_")
    demo_cols = [c for c in vcols if is_demo(c)]
    qcols = [(j, c) for j, c in enumerate(vcols) if c["name"] not in META_COLS and not is_demo(c)]
    qidx = [j for j, _ in qcols]

    # detect condition/arm columns up front (set by a randomizer, not a QSF question) so they
    # appear in the balance table and are excluded from the per-question distributions.
    # Condition/arm columns only exist when the survey actually randomizes (a survey-flow / even-
    # presentation randomizer assigns arms). Without randomization — e.g. the question-file ("RQ")
    # path — there are no arms, so low-cardinality survey questions must NOT be mistaken for
    # conditions. A column counts as a survey question when it links to ANY known question source
    # (QSF qid/tag, questions-dir, or question_logic), not just the QSF maps.
    qsf_qids = set(qsf_qid_map)
    has_randomization = bool(qsf and (qsf.get("rand_flow") or qsf.get("rand_even")))
    known_qids = qsf_qids | set(qmap) | set(ql_qmeta)
    cond_cols = []
    if has_randomization:
        for j, vc in qcols:
            ss = col_stats(vc["values"])
            is_question = base_qid(vc["qid"]) in known_qids or vc["name"] in qsf_tag_map
            if ss.get("kind") == "categorical" and 2 <= (ss.get("distinct") or 0) <= 8 and not is_question:
                counts = [t["count"] for t in ss.get("top", [])]
                cond_cols.append({"name": vc["name"], "cells": ss.get("top", []), "distinct": ss.get("distinct"),
                                  "balanced": bool(counts) and (max(counts) - min(counts)) <= max(1, 0.1 * max(counts))})
    cond_names = {c["name"] for c in cond_cols}

    # ---- embedded-data / assignment variables ----
    # Export columns matching an embedded-data field defined in the survey flow are
    # NOT questions: they are assignment markers (Condition, RealFake, ...) written by
    # randomizers/branches. They belong in the randomization section — with observed
    # cells compared against their theoretical exposure — never in the distributions,
    # where their structural blanks would read as missing data.
    emb_fields = qlogic.get("embedded_fields", {}) or {}
    assign_fields = qlogic.get("assignment_fields", {}) or {}
    emb_lower = {f.strip().lower(): f for f in emb_fields}
    emb_col_idx = {}
    for j, vc in qcols:
        f = emb_lower.get(vc["name"].strip().lower())
        if f and base_qid(vc["qid"]) not in known_qids:
            emb_col_idx[f] = j

    def _field_getter(r):
        def get(name):
            f = emb_lower.get(str(name).strip().lower())
            j = emb_col_idx.get(f) if f else None
            if j is None:
                return None
            v = str(vcols[j]["values"][r]).strip()
            return v or None
        return get

    qid_col = {}
    for j, vc in qcols:
        qid_col.setdefault(base_qid(vc["qid"]), j)

    def _answer_getter(r):
        def get(qid):
            j = qid_col.get(qid)
            if j is None:
                return None
            toks = set()
            for src in (vcols[j]["values"][r], (lcols[j]["values"][r] if j < len(lcols) else "")):
                s = str(src).strip()
                if s:
                    toks.add(s)
                    if is_num(s) and float(s) == int(float(s)):
                        toks.add(str(int(float(s))))
            return toks or None
        return get

    def _choice_tokens(qid, choice_key):
        meta = ql_qmeta.get(qid) or {}
        labels = meta.get("options") if isinstance(meta.get("options"), list) else []
        option_ids = (
            [str(value) for value in meta.get("option_ids", [])]
            if isinstance(meta.get("option_ids"), list)
            else [str(index + 1) for index in range(len(labels))]
        )
        recodes = (
            {str(key): str(value) for key, value in meta.get("recode_values", {}).items()}
            if isinstance(meta.get("recode_values"), dict)
            else {}
        )
        target = str(choice_key)
        for index, option_id in enumerate(option_ids):
            if option_id != target:
                continue
            expected = {recodes.get(option_id, option_id)}
            if index < len(labels) and str(labels[index]).strip():
                expected.add(str(labels[index]).strip())
            return expected
        return {target}

    emb_balance = []
    for f, j in sorted(emb_col_idx.items()):
        ss = col_stats(vcols[j]["values"], force_cat=True)
        nonblank_n = ss["n"] - ss["n_missing"]
        info = assign_fields.get(f)
        entry = {"name": f, "cells": ss.get("top", []), "distinct": ss.get("distinct"),
                 "kind": "assignment" if info else "embedded"}
        if info:
            counts = [t["count"] for t in ss.get("top", [])]
            entry["balanced"] = bool(counts) and (max(counts) - min(counts)) <= max(1, 0.1 * max(counts))
            note = f"embedded data — randomized assignment ({info.get('n_arms')} values)"
            if info.get("branch_cond") or info.get("branch_logic"):
                expected = None
                bl = info.get("branch_logic")
                if bl:
                    hits = [
                        eval_logic(
                            bl,
                            _field_getter(r),
                            _answer_getter(r),
                            _choice_tokens,
                        )
                        for r in range(n_rows)
                    ]
                    if any(h is not None for h in hits):
                        expected = sum(1 for h in hits if h)
                cond_txt = re.sub(r"^shown only when\s+", "", info.get("branch_cond") or "the enclosing branch matches")
                note += f" · set only when {cond_txt}"
                if expected is not None:
                    entry["expected"] = expected
                    note += f" — expected {expected}/{n_rows} by that condition, assigned {nonblank_n}/{n_rows}; blanks are structural"
                else:
                    note += f" — assigned {nonblank_n}/{n_rows} (branch-gated; blanks are structural)"
            else:
                note += f" — assigned {nonblank_n}/{n_rows}"
            entry["note"] = note
        else:
            entry["note"] = f"embedded data field — set for {nonblank_n}/{n_rows} twins"
        emb_balance.append(entry)
    emb_names = {vcols[j]["name"] for j in emb_col_idx.values()}
    cond_cols = [c for c in cond_cols if c["name"] not in emb_names]
    cond_names = {c["name"] for c in cond_cols} | emb_names

    fully_blank = sum(1 for r in range(n_rows) if all(str(vcols[j]["values"][r]).strip() == "" for j in qidx))
    sigs = ["||".join(str(vcols[j]["values"][r]) for j in qidx) for r in range(n_rows)]
    dup = len(sigs) - len(set(sigs))
    # Fully-skipped twins: every answerable cell blank AND at least one cell
    # flagged needs_validation — the twin was fielded but produced no usable
    # answer (all retries exhausted). Distinct from an all-structural blank row
    # and excluded from the effective sample size (a ghost respondent adds N
    # without signal). Prime rerun targets.
    fully_skipped = 0
    if scols is not None:
        fully_skipped = sum(
            1 for r in range(n_rows)
            if all(str(vcols[j]["values"][r]).strip() == "" for j in qidx)
            and any(str(scols[j]["values"][r]).strip() in _FLAGGED_STATUSES for j in qidx)
        )

    # ---- block-level allocation (survey-flow randomizer arms / branch blocks) ----
    # A question inside an allocation-randomized or branched block is only shown to the
    # twins routed into that block, so its sample is the block's allocation — rows with
    # >=1 nonblank answer among the block's columns — not the full run. Stats for those
    # variables are computed within the allocation, and each allocation randomizer
    # contributes a balance entry showing twins per arm.
    def _fielded(r, j):
        """Row r was FIELDED column j: a real answer, OR a flagged blank (skip
        or out-of-range) — a flag proves the twin was assigned/shown the
        question even though the value is blank, so allocation/exposure counts
        must include it."""
        if str(vcols[j]["values"][r]).strip() != "":
            return True
        return scols is not None and str(scols[j]["values"][r]).strip() in _FLAGGED_STATUSES

    col_block = {}   # column index -> block name
    block_cols = {}  # block name -> [column indexes]
    for j, vc in qcols:
        blk = (ql_qmeta.get(base_qid(vc["qid"])) or {}).get("block")
        if blk:
            col_block[j] = blk
            block_cols.setdefault(blk, []).append(j)
    conditional_candidates = set(qlogic["branch_blocks"]) | {b for g in qlogic["rand_groups"] for b in g["blocks"]}
    block_answered = {}   # block -> sorted rows with any nonblank answer in the block
    block_alloc = {}      # subset of block_answered that is genuinely allocation-restricted
    for blk in conditional_candidates:
        js = block_cols.get(blk)
        if not js:
            continue
        rows = sorted(r for r in range(n_rows) if any(_fielded(r, j) for j in js))
        block_answered[blk] = rows
        if 0 < len(rows) < n_rows:
            block_alloc[blk] = rows
    # A randomizer allocates arms only when it presents a SUBSET of its blocks
    # (SelectElements < #blocks) or the data shows restricted allocation; a randomizer
    # that presents all its blocks just shuffles order and gets no balance entry.
    rand_arm_balance, alloc_arm_blocks = [], set()
    for g in qlogic["rand_groups"]:
        arms = [b for b in g["blocks"] if block_cols.get(b)]
        if len(arms) < 2:
            continue
        sel = g.get("select")
        if not ((isinstance(sel, int) and 0 < sel < len(arms)) or any(b in block_alloc for b in arms)):
            continue
        alloc_arm_blocks.update(arms)
        cells = [{"label": b, "count": len(block_answered.get(b, [])),
                  "pct": round(100*len(block_answered.get(b, []))/n_rows, 1) if n_rows else 0.0} for b in arms]
        counts = [c["count"] for c in cells]
        unassigned = n_rows - len(set().union(*[set(block_answered.get(b, [])) for b in arms]))
        rand_arm_balance.append({"name": "Block randomizer — " + " / ".join(arms),
                                 "cells": cells, "distinct": len(cells),
                                 "balanced": (max(counts) - min(counts)) <= max(1, 0.1 * max(counts)),
                                 "unassigned": unassigned if unassigned > 0 else 0,
                                 "kind": "block_randomizer"})
    # blocks whose variables get allocation-scoped stats
    alloc_scope = {b: rows for b, rows in block_alloc.items()
                   if b in alloc_arm_blocks or b in qlogic["branch_blocks"]}

    # blocks excluded from THIS run, by block ID (two blocks can share a name). Where
    # the survey logic still holds them, their columns are kept blank for export
    # parity; they are left out of the distributions.
    _early_cfg = {}
    _mp = manifest_path or os.path.join(os.path.dirname(os.path.abspath(values_path)), "manifest.json")
    if os.path.exists(_mp):
        try:
            _early_cfg = (json.load(open(_mp)).get("config") or {})
        except Exception:
            _early_cfg = {}
    excluded_block_ids = set(excluded_block_ids_of(_early_cfg))
    # parents of multi-answer option columns (any sibling shows the 1/blank selection kind)
    # Parent QIDs of multi-answer option columns: any column encoded 1/blank with
    # at least one "1". The PARENT is base_qid (which strips the option index,
    # e.g. QID7_2 -> QID7), so sibling all-No option columns (QID7_8, no "1" of
    # their own) can be recognized as selection columns via the shared parent.
    selection_parents = set()
    for j, vc in qcols:
        vals = {str(x).strip() for x in vc["values"]}
        if ({"1", "1.0"} & vals) and vals <= {"", "1", "1.0"}:
            selection_parents.add(base_qid(vc["qid"]))

    variables = []; valid_flags = 0
    excluded_cols_by_block = {}   # block ID -> export columns omitted from distributions
    hidden_for_everyone = []      # columns of questions switched off ("If False") for every respondent
    for j, vc in qcols:
        if vc["name"] in cond_names:   # shown in the balance section, not the distributions
            continue
        lc = lcols[j] if j < len(lcols) else vc
        qlm = ql_qmeta.get(base_qid(vc["qid"])) or {}
        # a block excluded from this run keeps its export columns (parity with the
        # human export) but was never fielded — omit it from the distributions and
        # document it under "Excluded blocks" in the setting table instead. (Before
        # the "If False" check: a switched-off question in a dropped block is dropped.)
        if qlm.get("block_id") in excluded_block_ids:
            blk_id = qlm.get("block_id")
            excluded_cols_by_block[blk_id] = excluded_cols_by_block.get(blk_id, 0) + 1
            continue
        # a question (or its block's branch) switched off for everyone with "If
        # False" is kept as an empty column, as in a Qualtrics export, but was
        # asked of nobody: leave it out of the distributions and name it in the
        # fidelity notes rather than report it as 100% missing.
        _own_gate = (qlogic.get("display_logic_raw") or {}).get(base_qid(vc["qid"]))
        _col_blk = col_block.get(j)
        _blk_gate = (qlogic.get("branch_logic_raw") or {}).get(_col_blk) if _col_blk else None
        if logic_always_false(_own_gate) or logic_always_false(_blk_gate):
            hidden_for_everyone.append(vc["name"])
            continue
        # stats for a question in an allocation-randomized/branched block are computed
        # WITHIN the block's allocation — n is the arm's sample and missing is within-arm
        # missing, not the structural blanks of twins routed to the other arm.
        blk = col_block.get(j)
        alloc = alloc_scope.get(blk) if blk else None
        # exposure-aware scoping: a question gated by display logic, or living in a
        # branch-gated block, is only SHOWN to the twins whose own data satisfies the
        # condition (e.g. "Condition = 2 or 3"). Evaluate the gate per twin against
        # the exported embedded-data/answers; n and missing are computed within the
        # exposed group — blanks outside it are the design, not missing data.
        exposure_note = None
        # loop & merge expansion: a loop-prefixed column (3_QID7) exists only for
        # twins whose loop draw included that item — blanks are the loop design.
        is_loop_item = bool(re.match(r"^\d+_", str(vc["qid"] or "")) or re.match(r"^\d+_", vc["name"]))
        loop_never_drawn = False
        if is_loop_item:
            scope = alloc if alloc is not None else range(n_rows)
            answered = [r for r in scope if _fielded(r, j)]
            scope_n = len(alloc) if alloc is not None else n_rows
            if len(answered) == 0:
                loop_never_drawn = True
            elif len(answered) < scope_n:
                alloc = answered
                exposure_note = (f"loop & merge item — presented to {len(answered)}/{n_rows} twins "
                                 f"(per-twin loop draw); blanks are structural")
        gate = (qlogic.get("display_logic_raw") or {}).get(base_qid(vc["qid"]))
        block_gate = None
        if blk and (alloc is None) and blk in (qlogic.get("branch_logic_raw") or {}):
            block_gate = qlogic["branch_logic_raw"][blk]
        if gate or block_gate:
            scope_rows = alloc if alloc is not None else range(n_rows)
            exposed, decided = [], False
            for r in scope_rows:
                gf, ga = _field_getter(r), _answer_getter(r)
                shown = True
                for g in (block_gate, gate):
                    if not g:
                        continue
                    res = eval_logic(g, gf, ga, _choice_tokens)
                    if res is not None:
                        decided = True
                    if res is False:
                        shown = False
                        break
                if shown:
                    exposed.append(r)
            if decided and 0 < len(exposed) < (len(alloc) if alloc is not None else n_rows):
                alloc = exposed
                exposure_note = (f"conditionally shown — {len(exposed)}/{n_rows} twins satisfy the "
                                 f"display/branch condition; n and missing are within that group")
        vvals = [vc["values"][r] for r in alloc] if alloc else vc["values"]
        lvals = [lc["values"][r] for r in alloc] if alloc else lc["values"]
        stat_vals = None
        if scols is not None and j < len(scols):
            _sc = scols[j]["values"]
            stat_vals = [_sc[r] for r in alloc] if alloc else list(_sc)
        sv = col_stats(vvals, statuses=stat_vals)
        sl = col_stats(lvals, statuses=stat_vals)
        # link to the QSF question by QID (from the ImportId) first, then questions-dir, then export tag
        qmeta = qsf_qid_map.get(base_qid(vc["qid"])) or qmap.get(base_qid(vc["qid"])) or qsf_tag_map.get(vc["name"]) or {}
        qtype = qmeta.get("type") or qlm.get("qtype")
        is_text_field = "_TEXT" in (vc["qid"] or "") or "_TEXT" in vc["name"]
        nonblank_l = [str(x).strip() for x in lvals if str(x).strip() != ""]
        if sv.get("kind") == "selection":
            disp = sv
        elif qtype in ("MC", "Matrix") and not is_text_field:
            # single-answer MC/Matrix: a category distribution over the DISPLAY LABELS.
            # Never fall back to the values column just because the answered labels look
            # numeric ("2", "3") — the values may be stale Qualtrics recode IDs (97, 17, …)
            # that would render as junk categories.
            disp = col_stats(lvals if nonblank_l else vvals, force_cat=True, statuses=stat_vals)
        elif qtype in ("TE", "open_text") or is_text_field:
            # Precedence: (1) the QSF declares numeric validation -> numeric;
            # (2) otherwise infer numeric ONLY when every nonblank answer is one
            # complete scalar number; (3) any non-scalar answer (a typed range
            # like "0-5", a unit like "22 years") keeps the whole column as text,
            # verbatim — never split or coerced.
            declared_numeric = bool(qmeta.get("declared_numeric"))
            if declared_numeric or all_scalar_numbers(nonblank_l):
                disp = col_stats(lvals, statuses=stat_vals)          # numeric text entry (age, counts, …)
                # A freely typed number (age, counts, prices, durations) has no
                # fixed answer scale, so equal-width histogram bins are arbitrary
                # artifacts of the observed range — "22–26.3, 26.3–30.6, …" reads
                # as noise. Report the summary statistics only (mean/sd/min/
                # median/max), the same numbers a scale question reports.
                disp.pop("bins", None)
            else:
                # open text: character-length summary, NOT a category bar chart — unless
                # the answers concentrate on a few short options (brand recall etc.),
                # where the distribution over answers is the useful view. The repeat-ratio
                # only means something with enough answers; sparse slots (tail fields of a
                # list-up-to-N form) count as concentrated when short.
                cat = col_stats(lvals, force_cat=True, statuses=stat_vals)
                concentrated = (nonblank_l and st.mean([len(x) for x in nonblank_l]) <= 40
                                and (cat.get("distinct") or 0) <= 12
                                and ((cat.get("distinct") or 0) / len(nonblank_l) <= 0.6 or len(nonblank_l) < 10))
                disp = cat if concentrated else col_stats(lvals, force_text=True, statuses=stat_vals)
        elif sl.get("kind") in ("categorical", "text"):
            disp = sl
        else:
            disp = sv
        # Pair each display label with its response code from the values CSV, then order
        # `top` by the survey's own option order when we know it (padding zero-count
        # options so the full scale is visible); codes become scale positions so the
        # dashboard's ordinal charts/ramps stay in scale order even when the survey kept
        # stale Qualtrics recode IDs (96/97/98 for the low end of a 1–7 scale).
        opts = qlm.get("options")
        if disp.get("kind") == "categorical" and disp.get("top"):
            code_by_label = {}
            for code_raw, label_raw in zip(vvals, lvals):
                label, code = str(label_raw).strip(), str(code_raw).strip()
                if label and code and label not in code_by_label:
                    code_by_label[label] = code
            for t in disp["top"]:
                code = code_by_label.get(t["label"])
                if code is None and is_num(t["label"]):
                    code = t["label"]   # display labels ARE the codes (values-only column)
                if code is not None:
                    t["code"] = float(code) if is_num(code) else code
            if opts and qtype in ("MC", "Matrix") and not is_text_field and len(opts) <= 15:
                pos = {str(o).strip(): i for i, o in enumerate(opts)}
                have = {t["label"] for t in disp["top"]}
                for o in pos:
                    if o not in have:
                        disp["top"].append({"label": o, "count": 0, "pct": 0.0})
                for t in disp["top"]:
                    p = pos.get(t["label"])
                    if p is not None:
                        if t.get("code") is not None and t["code"] != float(p + 1):
                            t["value"] = t["code"]   # raw export code, kept for reference
                        t["code"] = float(p + 1)     # scale position drives the order
                disp["top"].sort(key=lambda t: (pos.get(t["label"], len(pos)), str(t["label"])))
                disp["scale_order"] = True
            elif all(isinstance(t.get("code"), float) for t in disp["top"]):
                disp["top"].sort(key=lambda t: t["code"])
                disp["scale_order"] = True
        flags = list(disp.get("flags", []))
        # constraint-aware validity: out-of-scope codes + out-of-range values from the QSF
        # declared constraints, plus min-length / content-format rules for open text —
        # counted (not just flagged) so they can be reported next to n and missing.
        con = qmeta.get("constraints") or {}
        nonblank = [str(x).strip() for x in vvals if str(x).strip() != ""]
        oos = {}
        if con.get("allowed") and sv.get("kind") != "selection" and not is_text_field and disp.get("kind") != "text":
            bad = [x for x in nonblank if x not in set(con["allowed"])]
            if bad:
                uniq = sorted(set(bad))
                oos["invalid"] = len(bad); oos["invalid_values"] = uniq[:6]
                flags.append(("error", f"{len(bad)} of {len(nonblank)} answers out of scope — "
                                       f"values not among the question's options: {', '.join(uniq[:6])} "
                                       f"(kept and flagged, never corrected)"))
        if (con.get("min") is not None or con.get("max") is not None):
            oor = [x for x in nonblank if is_num(x) and (
                (con.get("min") is not None and float(x) < con["min"]) or
                (con.get("max") is not None and float(x) > con["max"]))]
            if oor:
                rng = f'[{con.get("min")}, {con.get("max")}]'
                oos["out_of_range"] = len(oor)
                flags.append(("error", f"{len(oor)} value(s) out of declared range {rng}"))
        if qtype in ("TE", "open_text") or is_text_field:
            min_chars = qlm.get("min_chars")
            if min_chars and nonblank_l:
                short = [x for x in nonblank_l if len(x) < min_chars]
                if short:
                    oos["below_min_length"] = len(short)
                    flags.append(("error", f"{len(short)} answer(s) below the required minimum length ({min_chars} chars)"))
            ct = (qlm.get("content_type") or "").lower()
            if ct and nonblank_l:
                if "email" in ct:
                    bad_fmt = [x for x in nonblank_l if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", x)]
                elif "number" in ct or "numeric" in ct:
                    bad_fmt = [x for x in nonblank_l if not is_num(x)]
                elif "phone" in ct:
                    bad_fmt = [x for x in nonblank_l if len(re.sub(r"\D", "", x)) < 7]
                else:
                    bad_fmt = []
                if bad_fmt:
                    oos["format_mismatch"] = len(bad_fmt)
                    flags.append(("error", f"{len(bad_fmt)} answer(s) do not match the required format ({qlm.get('content_type')})"))
        if qlm.get("forced") and 2 < (disp.get("missing_pct") or 0) <= 5:
            flags.append(("caution", f"{disp['missing_pct']}% missing on a forced-response question"))
        sub = _sub_label(vc["text"])
        # structural blanks, not missingness: optional multi-slot text fields ("Brand 3" of a
        # list-up-to-ten form) are blank for twins who listed fewer entries, and an MC
        # "Other (please specify)" text column is blank unless that option was chosen.
        if ((qtype in ("TE", "open_text") and not qlm.get("forced") and sub)
                or (qtype in ("MC", "Matrix") and is_text_field)):
            flags = [f for f in flags if "missing" not in f[1] and f[1] != "column entirely empty"]
        gqid = base_qid(vc["qid"])
        # write-in companion columns (QID57_TEXT, QID8_7_TEXT): blank unless the twin
        # picked the write-in option — never missing data, regardless of whether the
        # parent question could be resolved. Attach to the parent question's card.
        # Only a CHOICE question has a write-in companion: a standalone text-entry
        # question also exports as QIDxx_TEXT (Qualtrics' naming), and treating it
        # as a write-in hid every skipped answer on it as "structural" ("skipped 0"
        # on a run where 18 of 20 twins returned nothing).
        is_write_in = bool(re.search(r"(?:_\d+)?_TEXT$", str(vc["qid"] or "")) or
                           re.search(r"(?:_\d+)?_TEXT$", vc["name"]))
        if is_write_in and str(qtype or "").strip().upper() in ("TE", "OPEN_TEXT", "NUMERIC"):
            is_write_in = False
        if is_write_in:
            flags = [f for f in flags if "missing" not in f[1]
                     and f[1] not in ("column entirely empty", "constant (no variation)")]
            parent = re.sub(r"(?:_\d+)?_TEXT$", "", gqid) or gqid
            gqid = parent
            n_filled = len(nonblank_l)
            var_note = f"write-in text for the option above — filled by {n_filled}/{n_rows} twins; blanks are structural"
        # Multi-answer option column (§8): recognized by PARENT metadata, not by
        # whether the column happens to contain a "1" — so an option NOBODY
        # selected is still a selection column, never "missing / empty". Within
        # the administered set (vvals is already alloc-scoped, so not_shown twins
        # are excluded), a "1" is SELECTED and a blank is NOT_SELECTED — a valid
        # negative, never structural or missing. selection_rate is a descriptive
        # stat; always/never-selected stay as chips but never drive report status.
        # A selection option: its parent has 1/blank option columns AND this column
        # is itself 1/blank-encoded (or all-blank). The second clause excludes an
        # "other (text)" write-in that shares the parent QID (e.g. QID7_8_TEXT) but
        # holds free text, not a selection code.
        _col_vals = {str(x).strip() for x in vc["values"]}
        is_selection_option = (base_qid(vc["qid"]) in selection_parents
                               and _col_vals <= {"", "1", "1.0"})
        never_selected = False
        if is_selection_option:
            n_admin = len(vvals)
            sel = sum(1 for v in vvals if str(v).strip() in ("1", "1.0"))
            never_selected = (sel == 0)
            disp = {
                "kind": "selection", "n": n_admin, "n_selected": sel, "n_total": n_admin,
                "select_rate": round(100 * sel / n_admin, 1) if n_admin else 0.0,
                "n_missing": 0, "missing_pct": 0.0, "n_valid": n_admin,
                "n_selected_cells": sel, "n_not_selected": n_admin - sel,
                "n_structural": 0, "n_skipped": 0, "n_out_of_range": 0,
                "n_cascade": 0, "skipped_pct": 0.0,
            }
            flags = []
            if n_admin and sel == 0:
                flags.append(("caution", "never selected"))
            elif n_admin and sel == n_admin:
                flags.append(("caution", "always selected"))
        if loop_never_drawn:
            flags = [f for f in flags if "missing" not in f[1]
                     and f[1] not in ("column entirely empty", "constant (no variation)")]
        var = {"name": vc["name"], "qid": vc["qid"], "qtype": qtype,
               "text": vc["text"] or qmeta.get("text", ""),
               "numeric": sv, "display": disp, "flags": flags, "constraints": con,
               # by-variable behavior: how the twins actually responded
               **({"behavior": {"n_valid": disp.get("n_valid", sv.get("n_valid")),
                                "n_skipped": disp.get("n_skipped", sv.get("n_skipped", 0)),
                                "n_out_of_range": disp.get("n_out_of_range", sv.get("n_out_of_range", 0)),
                                "n_cascade": disp.get("n_cascade", sv.get("n_cascade", 0)),
                                "n_not_selected": disp.get("n_not_selected", 0),
                                "n_structural": disp.get("n_structural", sv.get("n_structural", 0)),
                                "skipped_pct": disp.get("skipped_pct", sv.get("skipped_pct", 0.0))}}
                  if stat_vals is not None else {}),
               "block": qlm.get("block"), "settings_desc": qlm.get("settings"), "logic_desc": qlm.get("logic"),
               # Response requirement: the QSF is authoritative when supplied
               # (qmeta.forced_known), else question_logic.json, else unknown
               # (unknown stays conservative — see _skip_counts). For a write-in
               # "_TEXT" companion the requirement is the PARENT question's.
               "forced": bool(qmeta.get("forced")) if qmeta.get("forced_known") else bool(qlm.get("forced")),
               "forced_known": bool(qmeta.get("forced_known")) or bool(qlm),
               "group_qid": gqid, "group_text": qmeta.get("text") or vc["text"] or vc["name"],
               "sub_label": sub,
               "_order": (block_order.get(qlm.get("block"), 999), qlm.get("order", 9999), j)}
        if oos:
            var["oos"] = oos
        if is_write_in:
            var["write_in"] = True
            var.setdefault("notes", []).append(var_note)
        if never_selected:
            var.setdefault("notes", []).append("multi-answer option — never selected (selection rate 0%)")
        if loop_never_drawn:
            var.setdefault("notes", []).append(
                "loop & merge item — this loop slot was not drawn by any twin; blanks are structural")
        if alloc is not None:
            var["n_allocated"] = len(alloc)
            if exposure_note:
                var.setdefault("notes", []).append(exposure_note)
            else:
                arm = "randomizer arm" if blk in alloc_arm_blocks else "branch"
                var.setdefault("notes", []).append(
                    f"{arm} “{blk}” — {len(alloc)}/{n_rows} twins allocated; n and missing are within the arm")
        variables.append(var)

    # detect conditionally-displayed (complementary) columns — their "missingness" is structural,
    # not a data problem (e.g. two experimental arms shown to disjoint twins).
    nb_sets = {vc["name"]: {r for r in range(n_rows) if str(vc["values"][r]).strip() != ""} for j, vc in qcols}
    for var in variables:
        if var.get("n_allocated") is not None:   # already scoped to its block's allocation
            continue
        if not any(f[1].endswith("missing") for f in var["flags"]):
            continue
        a = nb_sets.get(var["name"], set())
        if not (0.2 <= (1 - len(a) / n_rows) <= 0.8):
            continue
        for other, b in nb_sets.items():
            if other == var["name"] or not b:
                continue
            overlap = len(a & b) / max(1, min(len(a), len(b)))
            if overlap < 0.05 and len(a | b) >= 0.9 * n_rows:
                var["flags"] = [f for f in var["flags"] if not f[1].endswith("missing")]
                var.setdefault("notes", []).append(f"conditionally displayed — complementary to {other}")
                break
    # "Clean" = no flags AND no flagged skips. Skips no longer live in `flags`
    # (they're structured on `behavior`), so count them explicitly here — a
    # variable many twins skipped is not a clean column.
    valid_flags = sum(
        1 for v in variables
        if not v["flags"]
        and not (_skip_counts(v) and int((v.get("behavior") or {}).get("n_skipped") or 0))
        and not int((v.get("behavior") or {}).get("n_out_of_range") or 0)
    )

    # tag each conditionally-displayed variable with which condition value gates it
    col_by_name = {c["name"]: c for j, c in qcols}
    cond_value_sets = {(cc["name"], t["label"]):
                       {r for r in range(n_rows)
                        if str(col_by_name.get(cc["name"], {}).get("values", [""] * n_rows)[r]).strip() == t["label"]}
                       for cc in cond_cols for t in cc["cells"]}
    for var in variables:
        if not var.get("notes"):
            continue
        a = nb_sets.get(var["name"], set())
        for (cname, cval), rs in cond_value_sets.items():
            if rs and a and len(a ^ rs) <= 0.05 * n_rows:
                var["notes"] = [f"shown only when {cname} = {cval}"]
                break

    # order the distribution variables by survey logic (block order → question order → column order)
    variables.sort(key=lambda v: v["_order"])

    # pooled "mentions" for grouped concentrated open-text fields (brand-recall style FORM
    # questions): the per-slot columns are near-duplicates of one distribution, so also
    # aggregate the share of twins (within the block's allocation) mentioning each answer.
    from collections import Counter
    name_to_j = {c["name"]: j for j, c in qcols}
    groups_seen = {}
    for v in variables:
        groups_seen.setdefault(v.get("group_qid"), []).append(v)
    for gqid, members in groups_seen.items():
        if len(members) < 3 or not all(m.get("qtype") in ("TE", "open_text") for m in members):
            continue
        js = [name_to_j[m["name"]] for m in members if m["name"] in name_to_j]
        if not js:
            continue
        rows = alloc_scope.get(col_block.get(js[0]), list(range(n_rows)))
        per_twin = []
        for r in rows:
            seen = {str(lcols[j]["values"][r]).strip() for j in js if j < len(lcols)} - {""}
            per_twin.append(seen)
        pooled = [x for s in per_twin for x in s]
        if not pooled:
            continue
        # pool only short, label-like answers that REPEAT across twins (brand recall);
        # judged on the pooled distribution, not per slot — later slots of a
        # list-up-to-N form are mostly unique on their own but the group isn't.
        distinct = len(set(pooled))
        if st.mean([len(x) for x in pooled]) > 40 or distinct > 25 or distinct / len(pooled) > 0.5:
            continue
        mention_counts = Counter()
        for s in per_twin:
            for x in s:
                mention_counts[x] += 1
        denom = len(rows) or 1
        # ties in label order: the sets above iterate in an order that changes from
        # one process to the next, which made a rebuilt report differ from the saved one
        top = sorted(mention_counts.items(), key=lambda kc: (-kc[1], kc[0]))[:15]
        members[0]["group_mentions"] = {
            "n": denom,
            "top": [{"label": k, "count": c, "pct": round(100*c/denom, 1)}
                    for k, c in top]}

    # per-question data issues (out-of-scope / range / format / min-length / real missing) —
    # surfaced in §3 Response validity next to the sample size, not just as card chips.
    validity_issues = []
    for v in variables:
        o = v.get("oos") or {}
        has_missing_flag = any("missing" in f[1] for f in v["flags"])
        gated = ("shown" in (v.get("logic_desc") or "")
                 or any("shown only when" in n or "conditionally displayed" in n for n in v.get("notes") or []))
        _b = v.get("behavior") or {}
        _blanks = int(_b.get("n_skipped") or 0)
        # A blank on a REQUIRED question is a forced-response omission; the same
        # blank on an optional question is a documented non-response, never a
        # fidelity failure. Both are reported, in separate columns.
        n_skipped = _blanks if _skip_counts(v) else 0
        n_optional_blank = 0 if _skip_counts(v) else _blanks
        n_out_of_range = int(_b.get("n_out_of_range") or 0)
        # CONSOLIDATED out-of-range: one user-facing number covering every answer
        # outside the admissible response domain — unmatched-and-blanked
        # (behavior.n_out_of_range), retained-but-not-an-option (oos.invalid),
        # outside the declared numeric range, and format/min-length violations.
        # max() over the two blanked/retained buckets avoids double counting a
        # cell that is reported in both; the domain violations are disjoint from
        # them (they are computed on nonblank, retained answers).
        oor_total = (max(n_out_of_range, int(o.get("invalid") or 0))
                     + int(o.get("out_of_range") or 0)
                     + int(o.get("below_min_length") or 0)
                     + int(o.get("format_mismatch") or 0))
        # A row appears when a cell was actually asked and came back missing or
        # unusable: a forced omission, an optional blank, or an out-of-range
        # answer. Structural non-exposure (an arm the twin was never assigned,
        # an option never selected) is NOT missingness and never lists a
        # question here. Optional blanks are listed as diagnostics but never
        # counted as fidelity failures (see _problematic).
        if not oor_total and not n_skipped and not n_optional_blank:
            continue
        _d = v["display"]
        _n_exposed = (int(_d.get("n") or 0) - int(_d.get("n_missing") or 0)) + _blanks + n_out_of_range
        validity_issues.append({
            "name": v["name"], "qid": v["qid"], "block": v.get("block"),
            # full text kept intact — the HTML shortens it on a word boundary and
            # exposes the whole string in a title attribute
            "text": (v.get("text") or v.get("group_text") or ""),
            # n = twins shown/eligible for this question (within-arm), NOT all rows
            "n": _n_exposed, "n_allocated": v.get("n_allocated"),
            # Missing = blank raw responses among exposed cells; reconciles to
            # forced omissions + optional blanks.
            "missing_n": n_skipped + n_optional_blank,
            "missing_pct": (round(100 * (n_skipped + n_optional_blank) / _n_exposed, 1)
                            if _n_exposed else 0.0),
            "forced_omissions": n_skipped,
            "optional_blanks": n_optional_blank,
            "out_of_range_total": oor_total,
            "invalid_values": o.get("invalid_values", []),
            # internal provenance, retained for the machine-readable record and
            # the per-variable cards even though the HTML shows one column
            "_detail": {"blanked_out_of_range": n_out_of_range,
                        "retained_out_of_scope": int(o.get("invalid") or 0),
                        "outside_declared_range": int(o.get("out_of_range") or 0),
                        "below_min_length": int(o.get("below_min_length") or 0),
                        "format_mismatch": int(o.get("format_mismatch") or 0)}})

    # persona coverage from demographic columns (panel composition + thin/gap flags)
    coverage_demos = []
    for c in demo_cols:
        ss = col_stats(c["values"])
        top = ss.get("top", [])
        coverage_demos.append({"name": c["name"].replace("demo_", ""),
                               "distinct": ss.get("distinct"), "missing_pct": ss.get("missing_pct"),
                               "top": top, "thin": [t["label"] for t in top if t["count"] < 20]})

    # fidelity (qsf) OR survey structure (questions dir)
    if qsf:
        fidelity = {"source": "qsf", "n_full": qsf["n_full"], "n_partial": qsf["n_partial"],
                    "n_unsupported": qsf["n_unsupported"], "warnings": list(qsf["warnings"]), "questions": qsf["questions"]}
        title, n_questions, type_counts = qsf["title"], qsf["n_questions"], qsf["type_counts"]
        logic = [k for k, v in qsf["features"].items() if v]
    else:
        qlist = [{"tag": qid, "qid": qid, "type": m["type"], "selector": "", "support": "Simulated", "text": m["text"]}
                 for qid, m in qmap.items()]
        fidelity = {"source": "questions", "n_full": len(qlist), "n_partial": 0, "n_unsupported": 0,
                    "warnings": ["Survey structure derived from per-twin question files; QSF feature-fidelity (display/skip/branch/media) requires the original .qsf."],
                    "questions": qlist}
        title, n_questions, type_counts, logic = "(derived from question files)", len(qmap), qtc, []
    if hidden_for_everyone:
        fidelity["warnings"].append(
            f"{len(hidden_for_everyone)} column(s) belong to questions switched off for every respondent "
            f"(display logic \"If False\"): {', '.join(hidden_for_everyone[:12])}"
            f"{' ...' if len(hidden_for_everyone) > 12 else ''}. They were asked of nobody, as in Qualtrics, "
            "and are left out of the answer distributions.")

    # Asked cells: answered + asked-but-blank + out-of-range, summed over every
    # answer-unit column. Excludes structural non-exposure (n_structural) and
    # cascade-hidden cells, neither of which was put to the twin.
    asked_cells_total = 0
    for _v in variables:
        _d = _v.get("display") or {}
        _b = _v.get("behavior") or {}
        _n = int(_d.get("n") or 0) - int(_d.get("n_missing") or 0)
        asked_cells_total += _n + int(_b.get("n_skipped") or 0) + int(_b.get("n_out_of_range") or 0)

    # Duplicate rows are deliberately NOT a status driver: identical answer rows
    # across respondents are possible by design (humans collide too on short
    # small-scale surveys) — they are reported as information in the validity
    # panel, never judged as a problem.
    # Report-level status = a THRESHOLD over the fraction of PROBLEMATIC questions
    # (real quality/process issues: out_of_range / genuine forced-skip / cascade /
    # out-of-scope / range / format), plus a separate threshold on the failed-call
    # fraction (twins whose whole call produced no usable answer = fully skipped).
    # Never any single flag; low-variance descriptive tags (always/never selected,
    # column-empty, constant) stay as per-question chips and do NOT drive status;
    # unsupported QSF features are informational, not a driver (owner decision).
    def _problematic(v):
        b = v.get("behavior") or {}
        # A skip only counts as a problem when it counts toward the skipping rate
        # (forced, or forced-ness unknown). A blank on a known-optional question is
        # a legitimate non-response, not a skip (§8.6 / round-4 §10).
        counted_skip = int(b.get("n_skipped") or 0) if _skip_counts(v) else 0
        if (int(b.get("n_out_of_range") or 0) or int(b.get("n_cascade") or 0)
                or counted_skip):
            return True
        o = v.get("oos") or {}
        return bool(o.get("invalid") or o.get("out_of_range")
                    or o.get("below_min_length") or o.get("format_mismatch"))

    def _sev(frac, caution_at, error_at):
        return "error" if frac >= error_at else ("caution" if frac >= caution_at else "ok")

    _rank = {"ok": 0, "caution": 1, "error": 2}
    total_q = len(variables)
    problematic_q = sum(1 for v in variables if _problematic(v))
    frac_q = problematic_q / total_q if total_q else 0.0
    sample_n = n_rows or 1
    call_fail_frac = (fully_skipped / sample_n) if scols is not None else 0.0
    q_sev = _sev(frac_q, REPORT_CAUTION_Q_FRAC, REPORT_ERROR_Q_FRAC)
    call_sev = _sev(call_fail_frac, REPORT_CAUTION_CALL_FRAC, REPORT_ERROR_CALL_FRAC)
    status = max((q_sev, call_sev), key=lambda s: _rank[s])
    status_basis = {
        "problematic_questions": problematic_q,
        "total_questions": total_q,
        "problematic_pct": round(100 * frac_q, 1),
        "failed_call_pct": (round(100 * call_fail_frac, 1) if scols is not None else None),
        "thresholds": {
            "caution_questions_pct": REPORT_CAUTION_Q_FRAC * 100,
            "error_questions_pct": REPORT_ERROR_Q_FRAC * 100,
            "caution_calls_pct": REPORT_CAUTION_CALL_FRAC * 100,
            "error_calls_pct": REPORT_ERROR_CALL_FRAC * 100,
        },
        "driver": ("failed_calls" if _rank[call_sev] > _rank[q_sev] else "questions"),
    }

    # run metadata: prefer the export manifest.json (config = the experiment design), then run_meta.json
    bundle_dir = os.path.dirname(os.path.abspath(values_path))
    mani = {}
    mpath = manifest_path or os.path.join(bundle_dir, "manifest.json")
    if os.path.exists(mpath):
        try: mani = json.load(open(mpath))
        except Exception: mani = {}
    cfg = mani.get("config", {}) or {}
    rc = mani.get("row_counts", {}) or {}
    run_status = mani.get("run_status", {}) or {}
    staged = mani.get("qsf_staged_runtime", {}) or {}
    # Remarks recorded while simulating (images left out, twins whose call
    # failed and were recorded as skipped) join the fidelity warnings, which is
    # where the results page already lists what the run could not replicate.
    for note in mani.get("simulation_warnings") or []:
        text = f"Run note: {str(note).strip()}"
        if str(note).strip() and text not in fidelity["warnings"]:
            fidelity["warnings"].append(text)
    rm = {}
    rmpath = os.path.join(bundle_dir, "run_meta.json")
    if os.path.exists(rmpath):
        try: rm = json.load(open(rmpath))
        except Exception: rm = {}

    # Prefer the run's research question as the survey title when the instrument itself
    # doesn't carry a name (question-files runs, planner-built surveys without a QSF).
    _rq = str(mani.get("research_question") or "").strip()
    if _rq and (not title or title == "(derived from question files)"):
        title = _rq

    files = {}
    SKIP_FILE_KINDS = {"wide_csv", "long_csv", "question_logic_json", "manifest_json", "bundle_zip"}   # not surfaced in the report
    if mani.get("files"):   # use the manifest's authoritative file list (kind + checksum)
        for f in mani["files"]:
            if f.get("kind") in SKIP_FILE_KINDS: continue
            files[f["file_name"]] = {"sha256": f.get("sha256", ""), "kind": f.get("kind")}
    else:
        for p in [values_path, labels_path, qsf_path]:
            if p: files[os.path.basename(p)] = {"sha256": sha256(p), "kind": None}
    # executed-logic traits + staged warnings + selected blocks (from manifest / question_logic)
    analysis = (staged.get("analysis") or {})
    traits = analysis.get("traits") or {}
    exec_traits = sorted(k for k, v in traits.items() if v is True)
    exec_warnings = analysis.get("warnings") or []
    _excluded_ids = excluded_block_ids_of(cfg)
    _names = {**{str(k): str(v) for k, v in (cfg.get("qsf_block_names") or {}).items()},
              **{bid: name for bid, name in excluded_blocks_of(cfg) if name},
              **{str(b["id"]): str(b["name"]) for b in qlogic["blocks"] if b.get("id")}}
    _all_names = [str(n) for n in _names.values()]
    # a name two blocks share is shown with its block ID
    excluded_blocks = [(_names.get(bid, bid) if _all_names.count(_names.get(bid, bid)) <= 1 else f"{_names[bid]} ({bid})")
                       for bid in _excluded_ids]
    selected_blocks = [b["name"] for b in qlogic["blocks"] if b.get("id") not in set(_excluded_ids)]

    # Effective run seed: explicit run seed if set, else the system default that the run actually
    # used (so the report always shows a reproducible seed, even when none was provided).
    _seed_user = cfg.get("seed")
    if _seed_user is None:
        _seed_user = rm.get("seed")
    _seed_explicit = _seed_user is not None
    _seed_effective = _seed_user if _seed_explicit else _DEFAULT_SELECTION_SEED
    setting = {"run_id": mani.get("run_id") or run_id,
               "generated_at": mani.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "mode": "qsf" if qsf else "bundle", "sample_size": rc.get("data_rows") or n_rows,
               # fielded twins minus fully-skipped ghosts (no usable answers at all);
               # this is the N real analyses should use
               "effective_sample_size": ((rc.get("data_rows") or n_rows) - fully_skipped)
                                        if scols is not None else None,
               "run_status": (run_status or None),
               "persona_bank": _persona_bank_label(cfg.get("persona_bank") or rm.get("persona_bank"), bool(cfg or rm)),
               "representation": cfg.get("persona_type") or rm.get("representation"),
               "model": cfg.get("model") or rm.get("model"),
               "filters": cfg.get("filters") if cfg else rm.get("filters"),
               "execution_profile": cfg.get("execution_profile"),
               "report_mode": cfg.get("report_mode"),
               "runtime_questions": rc.get("questions"),
               "execution": ({"mode": staged.get("execution_mode"), "stage_calls": staged.get("stage_calls"),
                              "response_rows": staged.get("response_rows")} if staged else None),
               "system_prompt": cfg.get("system_prompt") or rm.get("system_prompt") or _default_system_prompt(),
               "seed": _seed_effective, "seed_explicit": _seed_explicit,
               "blocks": selected_blocks, "excluded_blocks": excluded_blocks,
               "excluded_columns": sum(excluded_cols_by_block.values()),
               "hidden_for_everyone": hidden_for_everyone,
               "executed_logic": exec_traits, "execution_warnings": exec_warnings,
               "survey": {"title": title, "n_questions": n_questions, "type_counts": type_counts, "logic_features": logic},
               "files": files,
               "usage": mani.get("usage") or None,
               "note": None if (mani or rm) else "Experiment config (model / persona bank / representation / filters) not provided — add manifest.json to the bundle."}
    # randomization table rows: type · applies-to · count
    rand_rows = []
    if qsf and qsf.get("rand_flow"):
        arm_scope = "; ".join(" / ".join(g["blocks"]) for g in qlogic["rand_groups"])
        rand_rows.append({"type": "Survey-flow randomizer" + (" (even/balanced)" if qsf.get("rand_even") else ""),
                          "scope": arm_scope or (", ".join(cond_names) if cond_names else "survey flow / blocks"),
                          "count": len(qlogic["rand_groups"]) or len(cond_names) or 1})
    if qsf and qsf.get("rand_choice_count"):
        rand_rows.append({"type": "Choice/answer randomization", "scope": "per question (choice order)",
                          "count": qsf["rand_choice_count"]})
    report = {"schema_version": "0", "setting": setting, "status": status, "fidelity": fidelity,
              "randomization": {"table": rand_rows, "balance": rand_arm_balance + emb_balance + cond_cols},
              "validity": {"n_rows": n_rows, "duplicate_rows": dup,
                           "fully_skipped_rows": fully_skipped if scols is not None else None,
                           "n_questions": len(variables),
                           # ASKED CELLS: twin x answer-unit observations actually shown or
                           # requested = answered + asked-but-blank + out-of-range. Structural
                           # non-exposure (arm not assigned, option not selected, question not
                           # shown) is excluded, so this is NOT rows x columns.
                           "asked_cells_total": (asked_cells_total if scols is not None else None),
                           "skipped_cells_total": (sum(int((v.get("behavior") or {}).get("n_skipped") or 0)
                                                       for v in variables if _skip_counts(v)) if scols is not None else None),
                           "optional_blank_cells_total": (sum(int((v.get("behavior") or {}).get("n_skipped") or 0)
                                                             for v in variables if not _skip_counts(v)) if scols is not None else None),
                           "out_of_range_cells_total": (sum(int((v.get("behavior") or {}).get("n_out_of_range") or 0)
                                                            for v in variables) if scols is not None else None),
                           "cascade_cells_total": (sum(int((v.get("behavior") or {}).get("n_cascade") or 0)
                                                       for v in variables) if scols is not None else None),
                           # Complement of the problematic-question count, same definition, so
                           # the two percentages add to 100. Informational descriptive tags
                           # (constant column, always/never selected) are counted separately
                           # in `informational_flag_questions` and never as fidelity failures.
                           "questions_clean": len(variables) - problematic_q,
                           "pct_questions_clean": round(100*(len(variables) - problematic_q)/max(1, len(variables)), 1),
                           "informational_flag_questions": sum(1 for v in variables if v.get("flags")),
                           "status_basis": status_basis,
                           "issues": validity_issues},
              "distributions": {"variables": variables},
              "coverage": {"demographics": coverage_demos,
                           "note": (f"{len(coverage_demos)} demographic fields in panel. "
                                    "Comparison against a requested target distribution requires the run's panel filters "
                                    "(not present in this export bundle); thin cells (n<20) are flagged below."
                                    if coverage_demos else "No demographic fields detected.")}}
    json.dump(report, open(os.path.join(out_dir, "report.json"), "w"), indent=2)
    if emit_manifest:   # the export pipeline already writes the authoritative manifest.json
        json.dump(setting, open(os.path.join(out_dir, "manifest.json"), "w"), indent=2)
    open(os.path.join(out_dir, "report.html"), "w").write(render_html(report))
    return report


def write_report(export_dir, run_id="run"):
    """Backend entry point: read the files the export pipeline wrote into export_dir
    (responses_*.csv, manifest.json, question_logic.json) and emit report.html + report.json.
    Returns the report dict. Does NOT overwrite the export's manifest.json."""
    ed = str(export_dir)
    return build(
        os.path.join(ed, "responses_values.csv"),
        os.path.join(ed, "responses_labels.csv"),
        ed,
        qsf_path=None,
        manifest_path=os.path.join(ed, "manifest.json"),
        question_logic_path=os.path.join(ed, "question_logic.json"),
        run_id=run_id,
        emit_manifest=False,
    )

# ---------------- HTML renderer (self-contained, print-optimized) -------------
def esc(s):
    import html; return html.escape(str(s))


def _shorten(text, limit=80):
    """Escape and shorten for display, cutting on a WORD boundary (never mid-word
    like 'bran' or 'speci') and marking the cut with an ellipsis. The caller keeps
    the complete string in a title attribute, so nothing is lost."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return esc(clean)
    cut = clean[:limit]
    space = cut.rfind(" ")
    if space > int(limit * 0.5):          # only honour a boundary that is not too early
        cut = cut[:space]
    return esc(cut.rstrip(" ,;:.—-")) + "..."

def _fold(text, word_limit=30):
    """Long text (e.g. a full stimulus / question stem) collapses to a click-to-
    expand <details> after `word_limit` words; shorter text renders inline."""
    t = str(text or "")
    words = t.split()
    if len(words) <= word_limit:
        return esc(t)
    summary = " ".join(words[:word_limit])
    return (f'<details class="fold"><summary>{esc(summary)} …</summary>'
            f'<span>{esc(t)}</span></details>')

# categorical palette — same ramp as the dashboard's stacked demographic bars
PALETTE = ["#5b8bb0", "#7BA7C7", "#A3C4DC", "#C9A66B", "#B5838D", "#6D9886", "#9A8C98"]

def bar(pct, color="#5b8bb0", tip=None):
    t = f' title="{esc(tip)}"' if tip else ""
    return (f'<div class="barwrap"{t}><div class="bar" style="width:{min(100, pct)}%;background:{color}"></div>'
            f'<span>{pct}%</span></div>')

def _cat_tip(label, count, pct, n):
    return f"{label} — {count}/{n} twins ({pct}%)"

def render_html(rep):
    s = rep["setting"]; st_ = rep["status"]
    badge = {"ok": ("#1a7f37", "OK"), "caution": ("#9a6700", "CAUTION"), "error": ("#b3261e", "ATTENTION")}[st_]
    fid = rep["fidelity"]; val = rep["validity"]
    H = []
    H.append(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Simulation Report</title><style>
@page{{size:A4;margin:1.6cm}}*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;font-size:12px;line-height:1.5;margin:0;padding:28px 34px;max-width:1100px;background:#fff}}
h1{{font-size:20px;margin:0 0 2px;color:#171717;letter-spacing:-0.2px}}
h2{{font-size:14.5px;color:#1f4e73;background:#e9f4fd;border-bottom:1px solid #b3d4ee;padding:5px 8px;border-radius:6px;margin:24px 0 9px;page-break-after:avoid}}
h2::before{{content:"";display:inline-block;width:3px;height:12px;background:#5b8bb0;border-radius:2px;margin-right:8px}}
h3{{font-size:12.5px;margin:12px 0 4px;color:#262626}}.sub{{color:#737373;margin:0 0 8px}}
.badge{{display:inline-block;color:#fff;border-radius:12px;padding:2px 12px;font-weight:600;font-size:12px;background:{badge[0]}}}
table{{width:100%;border-collapse:collapse;margin:6px 0;font-size:10.5px;table-layout:fixed}}
th{{background:#d4e8f8;color:#1f4e73;text-align:left;padding:5px 8px;font-weight:600;border-bottom:1px solid #b3d4ee}}
td{{padding:5px 8px;border-bottom:1px solid #f0eeeb;vertical-align:top;overflow-wrap:anywhere}}
tr{{page-break-inside:avoid}}thead{{display:table-header-group}}
.chip{{display:inline-block;border-radius:8px;padding:1px 7px;font-size:9.5px;font-weight:600}}
.ok{{background:#d1fae5;color:#047857}}.caution{{background:#fef3c7;color:#b45309}}.error{{background:#ffe4e6;color:#be123c}}
.barwrap{{position:relative;background:#f0eeeb;border-radius:4px;height:14px;width:100%}}.bar{{height:14px;border-radius:4px}}
.barwrap span{{position:absolute;right:4px;top:-1px;font-size:9px;color:#333}}
.var{{border:1px solid #e5e4e1;border-radius:10px;padding:9px 12px;margin:9px 0;page-break-inside:avoid;background:#fff}}
.muted{{color:#737373;font-size:10px}}code{{font-family:Consolas,monospace;font-size:10px;background:#f5f4f2;padding:1px 4px;border-radius:3px}}
details.fold{{display:inline}}details.fold>summary{{display:inline;cursor:pointer;color:#2f5c7a;list-style:none}}details.fold>summary::-webkit-details-marker{{display:none}}details.fold[open]>summary{{color:#737373;font-size:10px}}
.statline{{font-family:inherit;color:#444;font-size:10.5px;margin:3px 0}}.qtext{{font-family:inherit;color:#666;font-size:10.5px;margin:2px 0}}
.sysprompt{{white-space:pre-wrap;font-family:Consolas,monospace;font-size:9.5px;color:#222;background:#faf9f7;border:1px solid #e5e4e1;border-radius:6px;padding:8px 10px;margin:4px 0 8px;overflow-wrap:anywhere}}
.tname{{font-weight:600}}.tcode{{color:#999;font-size:9px}}
.blockhead{{background:#e4f2fd;padding:5px 10px;border-radius:6px;color:#1f4e73;border-left:3px solid #5b8bb0}}
.demogrid{{display:grid;grid-template-columns:1fr 1fr;gap:14px 28px;margin:8px 0}}
.demoname{{font-size:11px;font-weight:600;color:#404040;margin:0 0 4px;text-transform:capitalize}}
.stack{{display:flex;height:16px;border-radius:6px;overflow:hidden;background:#f0eeeb}}
.seg{{height:100%}}
.legend{{margin-top:4px;font-size:9.5px;color:#525252;line-height:1.7}}
.legend .it{{display:inline-block;margin-right:10px;white-space:nowrap}}
.sw{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
@media print{{.demogrid{{grid-template-columns:1fr 1fr}}}}
</style></head><body>""")
    H.append(f"<h1>Simulation Report</h1>")
    _eff = s.get("effective_sample_size")
    _n_line = f'{s["sample_size"]} twins'
    if _eff is not None and _eff != s["sample_size"]:
        _n_line = f'{s["sample_size"]} twins fielded · {_eff} responded'
    H.append(f'<p class="sub">run <code>{esc(s["run_id"])}</code> · {esc(s["generated_at"])} · {_n_line}</p>')
    # run-outcome warning: a batch that failed/expired/timed out finished via
    # real-time recovery — visible up front, not buried in error_text
    _rs = s.get("run_status") or {}
    if str(_rs.get("batch_phase") or "") == "failed":
        _why = esc(str(_rs.get("error_text") or "batch did not complete"))
        _completed = str(_rs.get("status") or "").lower() == "completed"
        if _completed:
            # Only claim recovery when the run actually completed — otherwise
            # this would falsely reassure the reader about a failed run.
            H.append(f'<div style="background:#fff3e0;border:1px solid #f0b45f;border-radius:6px;'
                     f'padding:10px 14px;margin:10px 0"><b>Batch recovery:</b> the OpenAI batch for this run '
                     f'did not complete ({_why}); the affected twins were finished with real-time calls. '
                     f'Results are complete.</div>')
        else:
            H.append(f'<div style="background:#fdecea;border:1px solid #e0a0a0;border-radius:6px;'
                     f'padding:10px 14px;margin:10px 0"><b>Batch did not complete:</b> {_why}. This run’s '
                     f'status is <code>{esc(str(_rs.get("status") or "unknown"))}</code> — the results below '
                     f'may be incomplete. Re-run before relying on them.</div>')
    # setting
    sv = s["survey"]
    H.append("<h2>1. Experiment setting</h2><table><tbody>")
    dash = lambda x: esc(x) if x not in (None, "") else "—"
    H.append(f'<tr><th style="width:30%">Survey</th><td>{esc(sv["title"])}</td></tr>')
    H.append(f'<tr><th>Mode</th><td>{esc(s["mode"])}</td></tr>')
    H.append(f'<tr><th>Sample size</th><td>{s["sample_size"]} twins</td></tr>')
    if _eff is not None and _eff != s["sample_size"]:
        H.append(f'<tr><th>Effective sample</th><td>{_eff} twins '
                 f'<span class="muted">— excludes twins with no usable answers at all (fully skipped); '
                 f'use this N for analyses</span></td></tr>')
    H.append(f'<tr><th>Persona bank</th><td>{dash(s.get("persona_bank"))}</td></tr>')
    _rep_label = {"full": "Full persona", "summary": "Summary persona",
                  "demographics": "Demographics only"}.get(s.get("representation"), s.get("representation"))
    H.append(f'<tr><th>Representation</th><td>{dash(_rep_label)}</td></tr>')
    H.append(f'<tr><th>Model</th><td>{dash(s.get("model"))}</td></tr>')
    u = s.get("usage")
    if isinstance(u, dict) and u.get("total_tokens"):
        # Token usage only; the library has no prices.
        H.append(
            f'<tr><th>Token usage</th><td>'
            f'{u.get("total_tokens", 0):,} tokens '
            f'({u.get("input_tokens", 0):,} in + {u.get("output_tokens", 0):,} out'
            + (f', {u["reasoning_tokens"]:,} reasoning' if u.get("reasoning_tokens") else '')
            + f') across {u.get("calls", 0):,} API calls</td></tr>'
        )
    flt = s.get("filters")
    flt_txt = ", ".join(f"{k}: {v}" for k, v in flt.items()) if isinstance(flt, dict) and flt else ("none" if isinstance(flt, dict) else None)
    H.append(f'<tr><th>Panel filters</th><td>{dash(flt_txt)}</td></tr>')
    if s.get("execution_profile"):
        H.append(f'<tr><th>Execution profile</th><td>{esc(s["execution_profile"])} '
                 f'<span class="muted">— platform speed/quality preset (fast / balanced / deep) chosen when '
                 f'launching the run; affects planning depth only, not the survey logic</span></td></tr>')
    if s.get("report_mode"):
        H.append(f'<tr><th>Report mode</th><td>{esc(s["report_mode"])}</td></tr>')
    H.append(f'<tr><th>Seed</th><td>{dash(s.get("seed"))} <span class="muted">— the same seed reproduces the sample and the survey randomization</span></td></tr>')
    qtypes_full = ", ".join(f"{QTYPE_NAMES.get(k, k)}: {v}" for k, v in sv["type_counts"].items())
    rtq = f' → {s["runtime_questions"]} runtime items' if s.get("runtime_questions") else ''
    H.append(f'<tr><th>Questions</th><td>{sv["n_questions"]} survey questions{rtq} — {qtypes_full}</td></tr>')
    H.append(f'<tr><th>Logic features</th><td>{", ".join(sv["logic_features"]) or "—"}</td></tr>')
    if s.get("blocks"):
        H.append(f'<tr><th>Blocks ({len(s["blocks"])})</th><td>{esc(", ".join(s["blocks"]))}</td></tr>')
    if s.get("hidden_for_everyone"):
        _hid = s["hidden_for_everyone"]
        H.append(f'<tr><th>Switched off ({len(_hid)})</th>'
                 f'<td>{esc(", ".join(_hid))} '
                 f'<span class="muted">— display logic &ldquo;If False&rdquo; in the survey; asked of nobody, as in Qualtrics. '
                 f'The columns are kept empty in the CSVs and left out of the answer distributions below.</span></td></tr>')
    if s.get("excluded_blocks"):
        _nx = s.get("excluded_columns") or 0
        _cols_txt = (f' Their {_nx} export column{"s" if _nx != 1 else ""} are kept in the CSVs for column '
                     f'parity with a real Qualtrics export but omitted from the answer distributions below.'
                     if _nx else '')
        H.append(f'<tr><th>Excluded blocks ({len(s["excluded_blocks"])})</th>'
                 f'<td>{esc(", ".join(s["excluded_blocks"]))} '
                 f'<span class="muted">— deselected in this run configuration; never shown to the twins.{_cols_txt}</span></td></tr>')
    TRAIT = {"skip_logic": "Skip logic", "branch_logic": "Branch logic", "question_display_logic": "Display logic",
             "question_piped_text": "Piped text", "embedded_data_logic": "Embedded data", "randomizer": "Randomizer",
             "end_survey": "End-of-survey", "question_logic": "Question logic", "group": "Group"}
    if s.get("executed_logic"):
        H.append(f'<tr><th>Logic executed</th><td>{esc(", ".join(TRAIT.get(t, t) for t in s["executed_logic"]))}</td></tr>')
    FK = {"csv_values": "Response file — answers coded as values", "csv_labels": "Response file — answers as text labels",
          "wide_csv": "Wide CSV (one row per twin)", "long_csv": "Long CSV (one row per answer)",
          "question_logic_json": "Survey logic (blocks, questions, flow)", "manifest_json": "Run manifest",
          "bundle_zip": "Full bundle archive"}
    for fn, meta in s["files"].items():
        desc = FK.get(meta.get("kind")) or file_desc(fn)
        sh = f' <span class="muted">· sha256 {meta["sha256"][:12]}…</span>' if meta.get("sha256") else ''
        H.append(f'<tr><th>File</th><td><code>{esc(fn)}</code> · {desc}{sh}</td></tr>')
    H.append("</tbody></table>")
    if s.get("system_prompt"):
        H.append(f'<h3>System prompt</h3><pre class="sysprompt">{esc(s["system_prompt"])}</pre>')
    elif s.get("note"):
        H.append(f'<p class="muted">{esc(s["note"])}</p>')
    # fidelity
    H.append("<h2>2. QSF fidelity</h2>")
    _n_detected = max(0, len(fid.get("questions") or []) - fid["n_full"] - fid["n_partial"])
    H.append(f'<p>Support: <span class="chip ok">{fid["n_full"]} supported</span> '
             f'<span class="chip caution">{fid["n_partial"]} partially supported</span> '
             f'<span class="chip caution">{_n_detected} detected</span></p>')
    if fid["warnings"]:
        H.append("<h3>Warnings</h3><ul>" + "".join(f"<li>{esc(w)}</li>" for w in fid["warnings"]) + "</ul>")
    H.append('<table><colgroup><col style="width:13%"><col style="width:24%"><col style="width:11%"><col style="width:52%"></colgroup><thead><tr><th>Tag</th><th>Question type</th><th>Support</th><th>Question</th></tr></thead><tbody>')
    for q in fid["questions"]:
        cls = {"Full": "ok", "Partial": "caution", "Detected": "caution", "Unsupported": "caution"}.get(q["support"], "caution")
        _slabel = {"Full": "Supported", "Partial": "Partially supported", "Detected": "Detected",
                   "Unsupported": "Detected"}.get(q["support"], q["support"])
        tlabel = f'<span class="tname">{type_label(q["type"], q["selector"])}</span> <span class="tcode">({esc(q["type"])}/{esc(q["selector"])})</span>'
        H.append(f'<tr><td><code>{esc(q["tag"])}</code></td><td>{tlabel}</td><td><span class="chip {cls}">{_slabel}</span></td><td>{esc(q["text"])}</td></tr>')
    H.append("</tbody></table>")
    # validity
    H.append("<h2>3. Response validity</h2>")
    sb = val.get("status_basis") or {}
    _rows = val.get("n_rows") or 0
    _nq = val.get("n_questions") or 0
    _asked = val.get("asked_cells_total")
    _th = sb.get("thresholds", {}) if sb else {}

    def _pct(num, den):
        return f"{100*num/den:.1f}%" if den else "—"

    def _result(num, den):
        """count / denominator (percentage) — the one result format in this section."""
        if not den:
            return f"{num:,}"
        return f'{num:,} / {den:,} <span class="muted">({_pct(num, den)})</span>'

    def _plural(n, singular, plural=None):
        return singular if n == 1 else (plural or singular + "s")

    # ── study status ────────────────────────────────────────────────────────
    if sb:
        _status = str(rep.get("status", "ok"))
        _cls = {"ok": "ok", "caution": "caution", "error": "error"}.get(_status, "caution")
        _probq = sb.get("problematic_questions", 0)
        _fs = val.get("fully_skipped_rows") or 0
        _cq = _th.get("caution_questions_pct", 20)
        _eq = _th.get("error_questions_pct", 40)
        _cc = _th.get("caution_calls_pct", 10)
        _ec = _th.get("error_calls_pct", 30)
        H.append('<div class="methods" style="margin-bottom:10px">')
        H.append(f'<p><b>Study status: <span class="chip {_cls}">{esc(_status.upper())}</span></b></p>')
        H.append('<p>The overall status is based on two checks: response fields with fidelity failures '
                 'and twins with no usable responses. The more serious result determines the status.</p>')
        # Every status word renders as its own status tag, in the heading AND in
        # prose, so the three levels are recognisable at a glance and never read
        # as ordinary words.
        _chip_caution = '<span class="chip caution">CAUTION</span>'
        _chip_error = '<span class="chip error">ERROR</span>'
        H.append(f'<p>In this run, {_probq:,} of {_nq:,} {_plural(_nq, "response field")} '
                 f'({_pct(_probq, _nq)}) had a fidelity failure, and {_fs:,} of {_rows:,} '
                 f'{_plural(_rows, "twin")} ({_pct(_fs, _rows)}) returned no usable responses. '
                 f'The status changes to {_chip_caution} when these rates reach {_cq:g}% and '
                 f'{_cc:g}%, respectively, and to {_chip_error} at {_eq:g}% and {_ec:g}%.</p>')
        # The threshold above is evaluated on twins, not on provider calls; say so
        # only when genuine call-level failure data is absent (it always is today).
        _has_call_failures = sb.get("failed_calls") is not None
        _note = ('Informational labels, such as \u201cconstant response,\u201d appear in the '
                 'response-field details but do not affect the study status.')
        if not _has_call_failures:
            _note = ('Provider-call failures are not included because they are not recorded in this '
                     'export bundle. ') + _note
        H.append(f'<p class="muted">{_note}</p>')
        H.append('</div>')

    # ── run size ────────────────────────────────────────────────────────────
    _size = (f'<b>Run size:</b> {_rows:,} {_plural(_rows, "twin")} · '
             f'{_nq:,} {_plural(_nq, "response field")} (answer {_plural(_nq, "unit")})')
    if _asked is not None:
        _size += f' · {_asked:,} requested {_plural(_asked, "response")}'
    H.append(f'<p class="statline">{_size}</p>')
    H.append('<p class="muted">A response field is one answer expected by the survey. The number of '
             'requested responses may be smaller than the number of twins multiplied by the number of '
             'fields because branching and display logic mean that not every twin sees every field.</p>')

    # ── fidelity summary: level / check / result ────────────────────────────
    H.append('<table><colgroup><col style="width:13%"><col style="width:52%"><col style="width:35%">'
             '</colgroup><thead><tr><th>Level</th><th>Fidelity check</th><th>Result</th></tr></thead><tbody>')
    _fs = val.get("fully_skipped_rows")
    if _fs is not None:
        H.append(f'<tr><td>Twin</td><td><b>Twins with no usable responses</b> <span class="muted">— '
                 f'returned no usable answer to any question shown</span></td>'
                 f'<td>{_result(_fs, _rows)}</td></tr>')
    H.append(f'<tr><td>Twin</td><td><b>Identical response rows</b> <span class="muted">— informational '
             f'only and does not affect the study status</span></td>'
             f'<td>{_result(val.get("duplicate_rows") or 0, _rows)}</td></tr>')
    _probq = sb.get("problematic_questions", 0) if sb else 0
    H.append(f'<tr><td>Response field</td><td><b>Fields with fidelity failures</b> <span class="muted">— '
             f'contained a forced-response omission, an out-of-range answer, or a downstream question '
             f'hidden because an earlier required answer was missing</span></td>'
             f'<td>{_result(_probq, _nq)}</td></tr>')
    H.append(f'<tr><td>Response field</td><td><b>Fields without fidelity failures</b></td>'
             f'<td>{_result(val.get("questions_clean", _nq - _probq), _nq)}</td></tr>')
    if val.get("skipped_cells_total") is not None:
        H.append(f'<tr><td>Answer</td><td><b>Forced-response omissions</b> <span class="muted">— a required '
                 f'response was left blank</span></td>'
                 f'<td>{_result(val["skipped_cells_total"], _asked or 0)}</td></tr>')
    if val.get("optional_blank_cells_total") is not None:
        H.append(f'<tr><td>Answer</td><td><b>Optional blanks</b> <span class="muted">— an optional response '
                 f'was left blank; reported for transparency but not treated as a fidelity failure</span>'
                 f'</td><td>{_result(val["optional_blank_cells_total"], _asked or 0)}</td></tr>')
    if val.get("out_of_range_cells_total") is not None:
        H.append(f'<tr><td>Answer</td><td><b>Out-of-range answers</b> <span class="muted">— a nonblank '
                 f'response violated the allowed options, numeric range, or required format; the original '
                 f'response is retained for review</span></td>'
                 f'<td>{_result(val["out_of_range_cells_total"], _asked or 0)}</td></tr>')
    if val.get("cascade_cells_total"):
        H.append(f'<tr><td>Answer</td><td><b>Hidden by an earlier missing answer</b> <span class="muted">— '
                 f'display logic depended on a response that was blank or unmatched, so the hide is '
                 f'unreliable</span></td>'
                 f'<td>{_result(val["cascade_cells_total"], _asked or 0)}</td></tr>')
    H.append("</tbody></table>")
    _infl = val.get("informational_flag_questions") or 0
    if _infl:
        _lead = ("One additional response field has" if _infl == 1
                 else f"{_infl:,} additional response fields have")
        H.append(f'<p class="muted">{_lead} an informational label, such as \u201cconstant response.\u201d '
                 f'Informational labels appear on the response-field cards below but are not counted as '
                 f'fidelity failures.</p>')

    # ── response fields with missing or invalid answers ─────────────────────
    issues = val.get("issues") or []
    if issues:
        H.append(f'<h3>Response fields with missing or invalid answers ({len(issues)})</h3>'
                 '<p class="muted">This table includes response fields with at least one blank or invalid '
                 'answer. <b>Asked (n)</b> is the number of twins who saw the field, using the relevant '
                 'experimental arm when applicable. <b>Missing</b> is the sum of forced-response omissions '
                 'and optional blanks. Optional blanks are documented but do not count as fidelity '
                 'failures. Out-of-range answers are nonblank responses outside the permitted options, '
                 'numeric range, or format; the original response is retained for review.</p>')
        H.append('<table><colgroup><col style="width:12%"><col style="width:33%"><col style="width:8%">'
                 '<col style="width:11%"><col style="width:15%"><col style="width:11%"><col style="width:10%">'
                 '</colgroup><thead><tr><th>Response field</th><th>Question text</th>'
                 '<th class="num">Asked (n)</th><th class="num">Missing</th>'
                 '<th class="num">Forced-response omissions</th><th class="num">Optional blanks</th>'
                 '<th class="num">Out-of-range answers</th></tr></thead><tbody>')
        for i in issues:
            n_disp = f'{i["n"]}' + (' <span class="muted">(arm)</span>' if i.get("n_allocated") else "")
            _miss_n = i.get("missing_n")
            miss = ("—" if not _miss_n else
                    (f'{_miss_n:,} <span class="muted">({i.get("missing_pct")}%)</span>'
                     if _miss_n is not None else f'{i.get("missing_pct")}%'))
            fskip = (f'<span class="chip caution">{i["forced_omissions"]:,}</span>'
                     if i.get("forced_omissions") else "—")
            optb = f'{i["optional_blanks"]:,}' if i.get("optional_blanks") else "—"
            oor_n = i.get("out_of_range_total") or 0
            oor = f'<span class="chip error">{oor_n:,}</span>' if oor_n else "—"
            if oor_n and i.get("invalid_values"):
                oor += f' <span class="muted">({", ".join(esc(x) for x in i["invalid_values"])})</span>'
            _full = str(i.get("text") or "")
            H.append(f'<tr><td><code>{esc(i["name"])}</code></td>'
                     f'<td title="{esc(_full)}">{_shorten(_full, 80)}</td>'
                     f'<td class="num">{n_disp}</td><td class="num">{miss}</td><td class="num">{fskip}</td>'
                     f'<td class="num">{optb}</td><td class="num">{oor}</td></tr>')
        H.append("</tbody></table>")
    else:
        H.append('<p class="muted">No forced-response omissions, out-of-range answers, or problem '
                 'missingness detected.</p>')
    # randomization & balance
    rnd = rep.get("randomization", {})
    H.append("<h2>4. Randomization &amp; balance</h2>")
    if rnd.get("table"):
        H.append('<p class="statline">Randomization in this survey:</p>')
        H.append('<table><colgroup><col style="width:38%"><col style="width:42%"><col style="width:20%"></colgroup>'
                 '<thead><tr><th>Randomization type</th><th>Applies to</th><th>Count</th></tr></thead><tbody>')
        for r in rnd["table"]:
            H.append(f'<tr><td>{esc(r["type"])}</td><td>{esc(r["scope"])}</td><td>{r["count"]}</td></tr>')
        H.append("</tbody></table>")
    else:
        H.append('<p class="muted">No survey-flow or choice randomization detected in the QSF.</p>')
    if rnd.get("balance"):
        H.append('<p class="statline">Twins per condition cell (assignment balance):</p>')
        for cc in rnd["balance"]:
            if cc.get("balanced") is None and cc.get("kind") == "embedded":
                bal = '<span class="chip">embedded data</span>'
            elif cc.get("balanced"):
                bal = '<span class="chip ok">balanced</span>'
            else:
                bal = '<span class="chip caution">uneven</span>'
            H.append(f'<p class="statline"><b>{esc(cc["name"])}</b> {bal} · {cc.get("distinct")} cells</p>')
            if cc.get("note"):
                H.append(f'<p class="muted">{esc(cc["note"])}</p>')
            total = sum(t["count"] for t in cc["cells"]) or 1
            H.append('<table><colgroup><col style="width:40%"><col style="width:44%"><col style="width:16%"></colgroup>'
                     '<thead><tr><th>Cell</th><th>Share</th><th>Twins</th></tr></thead><tbody>')
            for t in cc["cells"]:
                tip = _cat_tip(t["label"], t["count"], t["pct"], total)
                H.append(f'<tr><td>{_fold(t["label"])}</td><td>{bar(t["pct"], tip=tip)}</td><td>{t["count"]}</td></tr>')
            H.append("</tbody></table>")
            if cc.get("unassigned"):
                H.append(f'<p class="muted">{cc["unassigned"]} twin(s) answered no question in any arm (end-of-survey or full missing).</p>')
    else:
        H.append('<p class="muted">No condition/arm assignment columns detected in the responses.</p>')
    # distributions — grouped by survey block, then by underlying question (expanded
    # multi-answer / matrix / rank / slider columns collapse into one card per question)
    H.append("<h2>5. Descriptive distributions</h2>")

    def oos_chips(v):
        o = v.get("oos") or {}
        bits = []
        if o.get("invalid"):
            bits.append(f'<span class="chip error" title="values: {esc(", ".join(o.get("invalid_values", [])))}">'
                        f'{o["invalid"]} out-of-scope answer{"s" if o["invalid"] != 1 else ""}</span>')
        if o.get("out_of_range"): bits.append(f'<span class="chip error">out-of-range {o["out_of_range"]}</span>')
        if o.get("below_min_length"): bits.append(f'<span class="chip error">below min length {o["below_min_length"]}</span>')
        if o.get("format_mismatch"): bits.append(f'<span class="chip error">format mismatch {o["format_mismatch"]}</span>')
        return (" " + " ".join(bits)) if bits else ""

    def body_full(d, extra=""):
        base = f'n={d["n"]} · missing {d["missing_pct"]}%'
        if d.get("n_skipped") is not None:
            # by-variable behavior: real answers vs flagged skips vs unflagged
            # blanks. "Unflagged blank" (not "structural"): mostly not-shown /
            # not-selected, but not asserted as such per-cell.
            base = (f'n={d["n"]} · answered {d.get("n_valid")}'
                    + (f' · <b>skipped {d["n_skipped"]}</b>' if d.get("n_skipped") else ' · skipped 0')
                    + (f' · <b>out-of-range {d["n_out_of_range"]}</b>' if d.get("n_out_of_range") else '')
                    + (f' · <b>cascade-hidden {d["n_cascade"]}</b>' if d.get("n_cascade") else '')
                    + (f' · unflagged blank {d.get("n_structural")}' if d.get("n_structural") else ''))
        if d["kind"] == "selection":
            tip = _cat_tip("Selected", d["n_selected"], d["select_rate"], d["n_total"])
            return (f'<p class="statline">selected by {d["n_selected"]}/{d["n_total"]} twins{extra}</p>'
                    f'<table><tbody><tr><td style="width:30%">Selected</td><td>{bar(d["select_rate"], tip=tip)}</td><td style="width:12%">{d["n_selected"]}</td></tr></tbody></table>')
        if d["kind"] == "numeric":
            out = (f'<p class="statline">{base} · mean {d.get("mean")} · sd {d.get("sd")} · min {d.get("min")}'
                   f' · median {d.get("median")} · max {d.get("max")}{extra}</p>')
            bins = d.get("bins") or []
            if bins:
                nn = (d["n"] or 0) - (d.get("n_missing") or 0)
                rows = "".join(f'<tr><td style="width:30%">{esc(b["label"])[:40]}</td>'
                               f'<td>{bar(b["pct"], tip=_cat_tip(b["label"], b["count"], b["pct"], nn))}</td>'
                               f'<td style="width:12%">{b["count"]}</td></tr>' for b in bins)
                out += f'<table><tbody>{rows}</tbody></table>'
            return out
        if d["kind"] == "text":
            return (f'<p class="statline">{base} · open text · characters — mean {d.get("mean_len")}'
                    f' · sd {d.get("sd_len")} · min {d.get("min_len")} · median {d.get("median_len")}'
                    f' · max {d.get("max_len")}{extra}</p>')
        nn = (d["n"] or 0) - (d.get("n_missing") or 0)
        rows = "".join(f'<tr><td style="width:45%">{esc(t["label"])[:48]}</td>'
                       f'<td>{bar(t["pct"], tip=_cat_tip(t["label"], t["count"], t["pct"], nn))}</td>'
                       f'<td style="width:12%">{t["count"]}</td></tr>' for t in d.get("top", []))
        return f'<p class="statline">{base} · {d.get("distinct")} distinct{extra}</p><table><tbody>{rows}</tbody></table>'

    vlist = rep["distributions"]["variables"]
    groups = []
    for v in vlist:
        if groups and groups[-1][0].get("group_qid") == v.get("group_qid"):
            groups[-1].append(v)
        else:
            groups.append([v])

    cur_block = object()
    for g in groups:
        head = g[0]
        if head.get("block") != cur_block:
            cur_block = head.get("block")
            H.append(f'<h3 class="blockhead">Block: {esc(cur_block or "—")}</h3>')
        grouped = len(g) > 1
        qt = f' · <span class="chip ok">{QTYPE_NAMES.get(head["qtype"], head["qtype"])}</span>' if head.get("qtype") else ''
        allflags = []
        for v in g:
            for f in v["flags"]:
                if f not in allflags:
                    allflags.append(f)
        chips = " ".join(f'<span class="chip {c}">{esc(m)}</span>' for c, m in allflags)
        # Skip chip rendered from the STRUCTURED behavior field, not a flag —
        # so it can never be lost to the structural-blank strippers.
        # the caution chip flags a problem, so known-optional blanks (not skips)
        # do not raise it. All members share one base_qid → same forced/known.
        group_skips = sum(int((v.get("behavior") or {}).get("n_skipped") or 0) for v in g if _skip_counts(v))
        if group_skips:
            chips += f' <span class="chip caution">{group_skips} skipped</span>'
        group_oor = sum(int((v.get("behavior") or {}).get("n_out_of_range") or 0) for v in g)
        if group_oor:
            chips += f' <span class="chip caution">{group_oor} out-of-range</span>'
        title = head.get("group_text") if grouped else head["name"]
        idtag = head.get("group_qid") if grouped else head["qid"]
        suffix = f" · {len(g)} items" if grouped else ""
        H.append(f'<div class="var"><h3>{esc(str(title))[:130]} <span class="muted">{esc(str(idtag))}{suffix}</span>{qt} {chips}</h3>')
        if not grouped:
            H.append(f'<p class="qtext">{esc(head["text"])[:140]}</p>')
        sl = " · ".join(x for x in (head.get("settings_desc"), head.get("logic_desc")) if x)
        if sl:
            H.append(f'<p class="muted">⚙ {esc(sl)}</p>')
        for note in head.get("notes", []):
            H.append(f'<p class="statline">↪ {esc(note)}</p>')
        kinds = {v["display"]["kind"] for v in g}
        gm = head.get("group_mentions") if grouped else None
        if gm:
            # pooled mentions across the group's slots (brand recall etc.) — one chart
            # instead of near-identical per-slot bars
            H.append(f'<p class="statline">Mentions pooled across {len(g)} fields — share of {gm["n"]} twins '
                     f'naming each answer at least once:</p>')
            H.append('<table><tbody>')
            for t in gm["top"]:
                tip = _cat_tip(t["label"], t["count"], t["pct"], gm["n"])
                H.append(f'<tr><td style="width:38%">{esc(t["label"])[:48]}</td>'
                         f'<td>{bar(t["pct"], tip=tip)}</td><td style="width:16%">{t["count"]}/{gm["n"]}</td></tr>')
            H.append('</tbody></table>')
            filled = ", ".join(f'{esc(str(v.get("sub_label") or v["name"]))} {(v["display"].get("n") or 0) - (v["display"].get("n_missing") or 0)}'
                               for v in g)
            H.append(f'<p class="muted">answers per field: {filled}</p>')
        elif not grouped:
            H.append(body_full(head["display"], extra=oos_chips(head)))
        elif kinds <= {"selection"}:
            H.append('<table><tbody>')
            for v in g:
                d = v["display"]; lab = v.get("sub_label") or v["name"]
                tip = _cat_tip(str(lab), d["n_selected"], d["select_rate"], d["n_total"])
                H.append(f'<tr><td style="width:38%">{esc(str(lab))[:48]}</td><td>{bar(d["select_rate"], tip=tip)}</td><td style="width:16%">{d["n_selected"]}/{d["n_total"]}</td></tr>')
            H.append('</tbody></table>')
        elif kinds <= {"numeric"}:
            means = [v["display"].get("mean") or 0 for v in g]
            mx = max(means) or 1
            H.append('<table><tbody>')
            for v in g:
                d = v["display"]; lab = v.get("sub_label") or v["name"]
                tip = f'{lab} — mean {d.get("mean")} · sd {d.get("sd")} · range {d.get("min")}–{d.get("max")} · n {d.get("n")}'
                H.append(f'<tr><td style="width:38%">{esc(str(lab))[:48]}</td><td>{bar(round(100*(d.get("mean") or 0)/mx, 1), tip=tip)}</td><td style="width:24%">mean {d.get("mean")} ({d.get("min")}–{d.get("max")})</td></tr>')
            H.append('</tbody></table>')
        else:
            for v in g:
                lab = v.get("sub_label") or v["name"]
                H.append(f'<p class="statline" style="font-weight:600">{esc(str(lab))[:60]}</p>')
                H.append(body_full(v["display"], extra=oos_chips(v)))
        H.append("</div>")
    H.append("<h2>6. Persona coverage</h2>")
    H.append(f'<p class="muted">{esc(rep["coverage"]["note"])}</p>')
    demos = rep["coverage"].get("demographics", [])
    if demos:
        H.append('<div class="demogrid">')
        for dem in demos:
            segs = [t for t in dem.get("top", []) if t["pct"] > 0]
            total = sum(t["count"] for t in dem.get("top", []))
            thin = [t for t in dem.get("top", []) if t["count"] < 20]
            flag = f' <span class="chip caution">{len(thin)} thin</span>' if thin else ''
            miss = f' <span class="muted">{dem.get("missing_pct")}% missing</span>' if (dem.get("missing_pct") or 0) > 0 else ''
            H.append(f'<div><p class="demoname">{esc(dem["name"].replace("_", " "))}{flag}{miss}</p>')
            H.append('<div class="stack">')
            for i, t in enumerate(segs):
                color = PALETTE[i % len(PALETTE)]
                tip = _cat_tip(t["label"], t["count"], t["pct"], total)
                H.append(f'<div class="seg" style="width:{max(0, t["pct"])}%;background:{color}" title="{esc(tip)}"></div>')
            H.append('</div><div class="legend">')
            for i, t in enumerate(segs):
                color = PALETTE[i % len(PALETTE)]
                H.append(f'<span class="it"><span class="sw" style="background:{color}"></span>'
                         f'{esc(t["label"])[:44]} <span class="muted">({t["pct"]}%)</span></span>')
            H.append('</div></div>')
        H.append('</div>')
    H.append("</body></html>")
    return "\n".join(H)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--qsf"); ap.add_argument("--questions"); ap.add_argument("--manifest")
    ap.add_argument("--question-logic"); ap.add_argument("--values", required=True)
    ap.add_argument("--labels"); ap.add_argument("--out", required=True); ap.add_argument("--run-id", default="prototype-run")
    a = ap.parse_args()
    rep = build(a.values, a.labels, a.out, qsf_path=a.qsf, questions_dir=a.questions, manifest_path=a.manifest,
                question_logic_path=getattr(a, "question_logic"), run_id=a.run_id)
    print("status:", rep["status"], "| variables:", len(rep["distributions"]["variables"]),
          "| fidelity:", rep["fidelity"]["n_full"], "full /", rep["fidelity"]["n_partial"], "partial /", rep["fidelity"]["n_unsupported"], "unsupported")
    print("wrote:", a.out + "/{report.json, manifest.json, report.html}")
