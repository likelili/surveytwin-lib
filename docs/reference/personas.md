# Persona banks

A persona bank holds the people your twins are made from. This page covers
opening a bank with `surveytwin.bank`, its representations and filters, how twins
are sampled, and the objects a bank returns. In a run, `configure` takes the bank
and the filters, and `plan` samples the twins from it.

Nothing on this page calls a model or needs an API key.

## What a bank is

A bank is a folder, or a zip of one, in the Croissant Twins format. What makes it
a bank is its `croissant.json` file: a Croissant dataset description whose
`conformsTo` list names the Croissant Twins 0.1 profile. The library looks for
`croissant.json` in the folder itself, in its `bank/` subfolder, or at most two
folders further down, and it must find exactly one. The data files it names must
be inside the bank, in JSON Lines, JSON, CSV or Parquet; Parquet needs `pyarrow`
(`pip install -e ".[parquet]"`).

Each persona in a bank has:

- an ID, such as `ex-03`;
- attributes, such as age and region. The bank marks some of them as
  filterable;
- one or more representations: texts that describe the same person in
  different ways, such as a short summary or demographics only.

When you open a bank, the library checks `croissant.json` against the Croissant
Twins rules. A failed check stops with a `BankError` that lists every failure; a
lesser problem is kept in `bank.warnings`. The bank is then read into a local
database in the cache folder. The next time you open it, the checks run again,
but the database is reused as long as the bank's files have not changed.

A bank's ID (`bank.id`) is the name of its package folder: the folder above
`bank/` when `croissant.json` sits in `bank/`, otherwise the folder that holds
`croissant.json`. For a zip with no package folder inside (`croissant.json` at
its top, or in a `bank/` folder at its top), it is the zip's name. The name is
lowercased, and any run of other characters becomes a hyphen.

## The default bank and the cache folder

The default bank is Twin-2K-500 in the Croissant Twins format (the
`twin-2k-500-ct` package). Its name is `"twin-2k-500"`: `surveytwin.bank()` opens
it, and so does `configure`, whose `bank` setting defaults to it. The package is
not published for download yet, so you tell the library where a copy is. These
environment variables are read when a bank is opened:

| Variable | What it does |
|---|---|
| `SURVEYTWIN_TWIN2K_BANK` | A local copy of the default bank: a folder or a zip. Checked first; `~` is expanded. |
| `SURVEYTWIN_TWIN2K_HF_REPO` | A Hugging Face dataset repository that holds the default bank. Used when `SURVEYTWIN_TWIN2K_BANK` is not set; the library downloads it into the cache folder (`hf/`), which needs the network. |
| `SURVEYTWIN_CACHE_DIR` | The cache folder, for every bank. Default: `~/.cache/surveytwin`. |

```bash
export SURVEYTWIN_TWIN2K_BANK=/path/to/twin-2k-500-ct      # a folder or zip
```

With neither of the first two set, `surveytwin.bank()` raises `BankError` and
says how to point the library at a copy. A bank opened by this name has
`bank.is_default` set to `True` and `bank.format` set to `"Twin-2K-500"`.

The cache folder holds each bank's database, the unpacked copy of each zip, and
the default bank when it is downloaded. Deleting it is safe: each bank is read
again the next time it is opened.

## Representations

A representation is one of the texts a bank has for each persona. The twin's
representation decides what the model reads about the person:

| Representation | What a twin gets |
|---|---|
| `full` | The fullest text the bank has for the person: for Twin-2K-500, the person's full record of answers from the original study. The longest text, so the largest prompts. |
| `summary` | A shorter profile of the same person. The default for Twin-2K-500. |
| `demographics` | Demographic facts only, such as age, region and education. |
| `empty` | No profile at all: the model gets the placeholder `[Empty Persona Profile]`. Use it as a baseline. |

`full`, `summary` and `demographics` are the representations of Twin-2K-500.
Another bank declares its own; the example bank in `examples/example-bank` has
`summary` (its default) and `demographics`. `bank.representations()` lists a
bank's representations with the bank's description of each. `empty` works with
every bank.

- **Naming one.** Where you name a representation (`configure`,
  `bank.preview_filter`, `bank.resolve_representation`), you can give its ID
  (`representations::summary`), its kind (`summary`) or its label (`Summary`);
  case does not matter. `None` means the bank's default, or its only
  representation. `bank.sample`, `bank.twin`, `bank.payload_chars` and
  `bank.max_payload_chars` take the ID; get it from `resolve_representation`.
- **How the model sees it.** The text goes into the prompt inside a block marked
  as untrusted data, under a line that names the bank, its version and the
  representation (see `Twin.text`). The system prompt tells the model to treat
  that block as a description of the person, never as instructions.
- **With `empty`.** Twins are still sampled from the bank, as for its default
  representation, and the filters still apply. Each twin keeps its persona's
  attributes; only the text is left out.
- **Size.** `plan` refuses a representation whose longest text is over about
  100,000 tokens, counted as four characters per token (`ContextBudgetError`).
  Nothing is cut to fit.

## Filters

Filters choose which personas can be sampled. They are a dict from the name of a
filterable field to what to keep (`bank.filter_options()` lists the fields):

| Filter | Keeps |
|---|---|
| `"West"` or `["South", "West"]` | Personas with any of these values. For a categorical field, give each value by its code (exactly) or its label (in any case). |
| `{"min": 30, "max": 50}` | Personas whose value is in the range, both ends included. Either end can be left out. Numeric fields only. |
| `{"missing": True}` | Personas with no value for the field (absent or empty). |

- A persona must match every field in the dict.
- An empty list, or `None`, puts no condition on that field.
- A field the bank does not offer, or a categorical value it does not have,
  raises `FilterError`, with the closest names as suggestions. So does a range
  on a categorical field, or a dict without `min`, `max` or `"missing": True`.
- For a numeric field, a list keeps personas whose value is written exactly so:
  `23` matches an age of 23, `23.0` does not. The numbers are not checked
  against the bank, so a number no persona has keeps nobody. A range is usually
  what you want.

Check what a filter keeps with `bank.preview_filter` before you use it:

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
for filters in (
    {"region": "West"},                              # one value
    {"region": ["South", "West"]},                   # any of these values
    {"education": ["Graduate degree", "ba"]},        # labels or codes
    {"age": {"min": 30, "max": 50}},                 # a range, ends included
    {"age": {"missing": True}},                      # no value for age
    {"region": ["West"], "age": {"max": 30}},        # every field must match
):
    print(filters, bank.preview_filter(filters).matching)
```

```text
{'region': 'West'} 2
{'region': ['South', 'West']} 4
{'education': ['Graduate degree', 'ba']} 4
{'age': {'min': 30, 'max': 50}} 3
{'age': {'missing': True}} 0
{'region': ['West'], 'age': {'max': 30}} 1
```

## Sampling and the seed

`plan` samples the twins with `bank.sample`, using the representation, filters,
sample size and seed of the run's settings. The eligible personas are those that
match the filters and have a text in the representation (with `empty`, a text in
the bank's default representation). They are taken in ID order, and the sample
is drawn from them with Python's random number generator, started from the
seed.

- The same bank files, representation, filters, sample size and seed give the
  same twins, in the same order, on any machine.
- A different sample size can give different twins: with the same seed, a
  3-twin sample is not always the first 3 twins of a 4-twin sample (the example
  under `Bank.sample` shows one). To try a few twins of a large run, plan the
  large run and take `plan.head(n)`.
- When fewer personas are eligible than you ask for, all of them are used, in ID
  order, and `plan` adds a warning.
- The seed also decides every random choice in the survey: randomizer arms,
  question and choice orders, and random numbers (see
  [Configuring and planning a run](configure-and-plan.md)).

## `bank`

```python
surveytwin.bank(name_or_path: str | os.PathLike[str] = 'twin-2k-500') -> Bank
```

Opens a persona bank: the default bank, or a Croissant Twins folder or zip.

**Parameters**

- `name_or_path` (`str | os.PathLike[str]`, default `'twin-2k-500'`): `'twin-2k-500'`
  opens the default bank, from the copy that `SURVEYTWIN_TWIN2K_BANK` or
  `SURVEYTWIN_TWIN2K_HF_REPO` points to. Anything else is the path of a bank
  folder or a `.zip` file; `~` is expanded. A zip is unpacked into the cache
  folder.

**Returns** `Bank`: the opened bank.

**Raises**

- `BankError`: the default bank is asked for and neither variable is set; the
  path does not exist, or is a file other than a zip; a zip holds a path that
  leads outside it; no `croissant.json` is found, or more than one;
  `croissant.json` cannot be read, does not name the Croissant Twins 0.1
  profile, or fails a Croissant Twins check; the bank's data cannot be read.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
print(bank)
```

```text
Example Bank (example-bank v1.0.0), 8 personas
Representations: Summary (default), Demographics
Filterable fields: age, region, education
```

## `Bank`

An opened persona bank. You get one from `surveytwin.bank(...)`; `configure`
opens one for you when you give it a name or a path, and keeps it in
`config.bank`. Treat its attributes as read-only. Printing a bank shows its
name, size, representations, filterable fields and any warnings.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `id` | `str` | The bank's ID, from its package folder's name. |
| `name` | `str` | The bank's name, from `croissant.json`. |
| `version` | `str` | The bank's version, from `croissant.json`. |
| `size` | `int` | How many personas the bank holds. |
| `format` | `str` | `"Twin-2K-500"` for the default bank, `"Croissant Twins"` for any other. |
| `is_default` | `bool` | Whether the bank was opened as the default bank. |
| `default_representation` | `str \| None` | The ID of the representation the bank marks as its default; `None` if it marks none. |
| `persona_basis` | `str` | What the bank says its personas are based on, such as `SyntheticPersona`. |
| `fingerprint` | `str` | A 16-character hash of the bank's file names and contents. The same files give the same fingerprint on any machine, from a folder or a zip. It is saved with results. |
| `source` | `Path` | What was opened: the path you gave, or the default bank's copy. |
| `bank_dir` | `Path` | The folder that holds `croissant.json`; for a zip, inside the cache folder. |
| `warnings` | `list[str]` | Lesser problems the Croissant Twins checks found, each as `"[code] message"`. |
| `description` | `dict[str, Any]` | The bank's card: `bank_id`, `version`, `name`, `publisher`, `license`, `citation`, `persona_basis`, `persona_count` and `conditions_of_access`. |

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
print(repr(bank))
print(bank.id, bank.size, bank.default_representation)
print(bank.description["license"])
```

```text
Bank('example-bank' v1.0.0, 8 personas, Croissant Twins)
example-bank 8 representations::summary
https://creativecommons.org/publicdomain/zero/1.0/
```

### `Bank.representations`

```python
Bank.representations() -> list[Representation]
```

Every representation the bank declares, as `Representation` records. The list
prints as a table, with the bank's description of each representation.

**Returns** `list[Representation]`: one record per representation (`empty` is not
listed; every bank has it).

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
reps = bank.representations()
print(reps)
print(reps[1].id, reps[1].kind, reps[1].coverage)
```

```text
Representations:
  Summary       default  8 personas  representations::summary
  Demographics           8 personas  representations::demographics
  Summary: A short paragraph about the person: work, household, habits and views.
  Demographics: Age, region and education only.
representations::demographics demographics 8
```

### `Bank.resolve_representation`

```python
Bank.resolve_representation(representation: str | None) -> str
```

Finds the representation you name and returns its ID.

**Parameters**

- `representation` (`str | None`): a representation's ID, kind or label, in any
  case; `None` for the bank's default (or its only representation); or
  `"empty"`, which stays `"empty"`.

**Returns** `str`: the representation's ID, or `"empty"`.

**Raises**

- `BankError`: no representation matches, or more than one does; the bank
  declares no representations; `None` is given and the bank has several and
  marks none as its default.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
print(bank.resolve_representation(None))            # the default
print(bank.resolve_representation("demographics"))  # by kind
print(bank.resolve_representation("Summary"))       # by label
print(bank.resolve_representation("empty"))
```

```text
representations::summary
representations::demographics
representations::summary
empty
```

### `Bank.representation_label`

```python
Bank.representation_label(view_id: str) -> str
```

The label of a representation, as printed in run settings and reports.

**Parameters**

- `view_id` (`str`): a representation ID, as `resolve_representation` returns
  it, or `"empty"`.

**Returns** `str`: the label, such as `"Summary"`; `"Empty persona"` for
`"empty"`.

**Raises**

- `LookupError`: `view_id` is not one of the bank's representation IDs. (This
  error is not a `SurveytwinError`.)

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
print(bank.representation_label("representations::demographics"))
print(bank.representation_label("empty"))
```

```text
Demographics
Empty persona
```

### `Bank.filter_options`

```python
Bank.filter_options() -> FilterOptions
```

The bank's filterable fields: for a categorical field, its values and how many
personas have each; for a numeric field, its smallest and largest value.

**Returns** `FilterOptions`: one `FilterField` per filterable field, in the
bank's order.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
options = bank.filter_options()
print(options)
print(options.names())
for field in options:
    print(field.name, field.type, field.min, field.max, [v["value"] for v in field.values])
```

```text
3 filterable fields:
  age        Age        numeric      23 to 70
  region     Region     categorical  Northeast (2), Midwest (2), South (2), West (2)
  education  Education  categorical  High school (2), Some college (2), Bachelor's degree (2), Graduate degree (2)
['age', 'region', 'education']
age numeric 23.0 70.0 []
region categorical None None ['Northeast', 'Midwest', 'South', 'West']
education categorical None None ['hs', 'some_college', 'ba', 'grad']
```

### `Bank.normalize_filters`

```python
Bank.normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]
```

Checks a filter dict and turns it into the form the bank's database reads:
`{"op": "in", "values": [...]}` for values (each label replaced by its code),
`{"op": "range", "gte": ..., "lte": ...}` for a range, and
`{"op": "is_missing", "value": True}` for `{"missing": True}`. You rarely need it,
since `configure`, `preview_filter` and `sample` call it, but it is a quick way to
check a filter.

**Parameters**

- `filters` (`dict[str, Any] | None`): filters in the form described under
  [Filters](#filters). `None` or `{}` gives `{}`.

**Returns** `dict[str, Any]`: the same fields, each with its condition in the
database's form.

**Raises**

- `FilterError`: `filters` is not a dict; it names a field the bank does not
  offer, or a categorical value the field does not have (with suggestions); it
  gives a range for a categorical field; a dict condition has neither `min`,
  `max` nor `"missing": True`.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
print(bank.normalize_filters({"education": ["Graduate degree", "ba"], "age": {"min": 30}}))
try:
    bank.normalize_filters({"regoin": ["West"]})
except st.FilterError as error:
    print(error)
```

```text
{'education': {'op': 'in', 'values': ['grad', 'ba']}, 'age': {'op': 'range', 'gte': 30, 'lte': None}}
Bank example-bank has no filterable field 'regoin'. Did you mean: region, education?
```

### `Bank.preview_filter`

```python
Bank.preview_filter(
    filters: dict[str, Any] | None = None,
    *,
    representation: str | None = None,
    sample_size: int | None = None,
) -> AudiencePreview
```

Counts the personas a filter keeps, before you use it in a run.

**Parameters**

- `filters` (`dict[str, Any] | None`, default `None`): the filters to check, in
  the form described under [Filters](#filters). `None` keeps every persona.
- `representation` (`str | None`, default `None`): only personas with a text in
  this representation count, as only they can be sampled. Give its ID, kind or
  label, as for `configure`; `None` is the bank's default. With `"empty"`, every
  persona that matches the filters counts.
- `sample_size` (`int | None`, default `None`): the sample size you plan to use.
  When given, `covers_sample_size` says whether enough personas match.

**Returns** `AudiencePreview`: the counts.

**Raises**

- `FilterError`: as for `normalize_filters`.
- `BankError`: as for `resolve_representation`.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
audience = bank.preview_filter({"region": ["South", "West"], "age": {"max": 50}}, sample_size=3)
print(audience)
print(audience.matching, audience.share, audience.by_value)
```

```text
3 of 8 personas match (37.5%), with representation representations::summary
  region: South 1, West 2
  enough for the sample size: yes
3 0.375 {'region': {'South': 1, 'West': 2}}
```

### `Bank.sample`

```python
Bank.sample(
    *,
    representation: str,
    filters: dict[str, Any] | None,
    n: int,
    seed: int,
) -> list[str]
```

The persona IDs a sample with these settings picks, in order (see
[Sampling and the seed](#sampling-and-the-seed)). `plan` calls it with the run's
settings, so calling it yourself shows which personas a plan will pick.

**Parameters**

- `representation` (`str`): a representation ID, as `resolve_representation`
  returns it, or `"empty"`. Only personas with a text in it are eligible; with
  `"empty"`, those with a text in the bank's default representation.
- `filters` (`dict[str, Any] | None`): the filters, or `None` for none.
- `n` (`int`): how many to sample. Fewer come back when fewer are eligible; `0`
  or less gives an empty list.
- `seed` (`int`): starts the random number generator. Any whole number works
  here; `configure` asks for 0 or more.

**Returns** `list[str]`: the sampled persona IDs, in sample order.

**Raises**

- `FilterError`: as for `normalize_filters`.
- `LookupError`: `representation` is not a representation ID (a kind or a label
  is not enough here). This error is not a `SurveytwinError`.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
view = bank.resolve_representation(None)
print(bank.sample(representation=view, filters={"region": ["South", "West"]}, n=3, seed=7))
print(bank.sample(representation=view, filters=None, n=3, seed=7))
print(bank.sample(representation=view, filters=None, n=4, seed=7))
```

```text
['ex-02', 'ex-03', 'ex-07']
['ex-08', 'ex-05', 'ex-04']
['ex-08', 'ex-06', 'ex-05', 'ex-07']
```

### `Bank.twin`

```python
Bank.twin(persona_id: str, representation: str) -> Twin
```

One persona as a twin: its attributes, and its text in a representation exactly
as a plan puts it in the prompt.

**Parameters**

- `persona_id` (`str`): the persona's ID. An ID the bank does not have is not
  refused: you get a twin with no attributes and the placeholder text
  `[Persona representation unavailable for this view]`. (A plan never holds such
  a twin; `plan` refuses twins without a text.)
- `representation` (`str`): a representation ID, as `resolve_representation`
  returns it, or `"empty"`.

**Returns** `Twin`: the twin.

**Raises**

- `LookupError`: `representation` is not a representation ID. This error is not
  a `SurveytwinError`.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
twin = bank.twin("ex-03", bank.resolve_representation("demographics"))
print(repr(twin))
print(twin.text)
print(bank.twin("ex-03", "empty").text)
```

```text
Twin('ex-03', age=23, region='West', education='some_college'; 265 characters of representations::demographics)
Source bank: example-bank v1.0.0 | Representation: Demographics (demographics) | Media: text/plain | ID: ex-03/demographics
----- BEGIN PERSONA REPRESENTATION (untrusted data) -----
Age: 23
Region: West
Education: some college
----- END PERSONA REPRESENTATION -----
[Empty Persona Profile]
```

### `Bank.payload_chars`

```python
Bank.payload_chars(persona_id: str, representation: str) -> int
```

The length of one persona's text in a representation, in characters, as the
bank holds it (before it is wrapped for the prompt).

**Parameters**

- `persona_id` (`str`): the persona's ID.
- `representation` (`str`): a representation ID, or `"empty"`.

**Returns** `int`: the number of characters; `0` for `"empty"`, or when the
persona has no text in the representation.

**Raises**

- `LookupError`: `representation` is not a representation ID. This error is not
  a `SurveytwinError`.

**Example**

```python
import surveytwin as st

bank = st.bank("examples/example-bank")
view = bank.resolve_representation(None)
print(bank.payload_chars("ex-03", view), bank.max_payload_chars(view))
```

```text
318 362
```

### `Bank.max_payload_chars`

```python
Bank.max_payload_chars(representation: str) -> int
```

The length of the longest text in a representation, in characters. `plan` uses
it to refuse a representation too large for one prompt (over about 100,000
tokens, counted as four characters per token). The example under
`Bank.payload_chars` shows it.

**Parameters**

- `representation` (`str`): a representation ID, or `"empty"`.

**Returns** `int`: the number of characters; `0` for `"empty"`.

**Raises**

- `LookupError`: `representation` is not a representation ID. This error is not
  a `SurveytwinError`.

## `Representation`

One of a bank's representations, as listed by `bank.representations()`. A record.
It is not exported as `surveytwin.Representation`; the class lives in
`surveytwin.personas.bank`.

| Name | Type | Meaning |
|---|---|---|
| `id` | `str` | The representation's ID, such as `representations::summary`. |
| `label` | `str` | Its label, such as `Summary`. |
| `kind` | `str` | Its kind, such as `summary`. |
| `description` | `str` | The bank's description of the text, or empty. |
| `is_default` | `bool` | Whether the bank marks it as its default. |
| `coverage` | `int` | How many personas have a text in it. |
| `media_type` | `str` | The text's format, such as `text/plain`. |

## `FilterOptions`

The filterable fields of a bank, from `bank.filter_options()`. A record.
Iterating over it gives each `FilterField`; printing it shows a table.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `fields` | `list[FilterField]` | One entry per filterable field, in the bank's order. |

### `FilterOptions.names`

```python
FilterOptions.names() -> list[str]
```

The names of the filterable fields, in the bank's order: the names to use as
keys in a filter dict.

**Returns** `list[str]`: the field names.

**Example**

```python
import surveytwin as st

print(st.bank("examples/example-bank").filter_options().names())
```

```text
['age', 'region', 'education']
```

## `FilterField`

One filterable field, in `FilterOptions.fields`. A record. It is not exported as
`surveytwin.FilterField`; the class lives in `surveytwin.personas.bank`.

| Name | Type | Meaning |
|---|---|---|
| `name` | `str` | The field's name, as used in a filter dict. |
| `label` | `str` | The field's label, such as `Age`. |
| `type` | `str` | `"numeric"` or `"categorical"`. |
| `values` | `list[dict[str, Any]]` | For a categorical field, each value as a dict: `value` (the code), `label` and `count` (how many personas have it). The values the bank lists for the field come first, in its order, then any other value found in the data; a field with no list has the values found, in alphabetical order. Empty for a numeric field. |
| `min` | `float \| None` | For a numeric field, the smallest value in the bank; `None` if no persona has one, and for a categorical field. |
| `max` | `float \| None` | For a numeric field, the largest value in the bank; `None` as for `min`. |

## `AudiencePreview`

What a filter keeps, from `bank.preview_filter(...)`. A record; printing it gives
a short summary (see the example under `Bank.preview_filter`).

| Name | Type | Meaning |
|---|---|---|
| `matching` | `int` | How many personas match the filters and have a text in the representation. With `empty`, every persona that matches the filters. |
| `total` | `int` | How many personas the bank holds. |
| `share` | `float` | `matching` divided by `total`, rounded to six decimal places. |
| `by_value` | `dict[str, dict[str, int]]` | For each field filtered by a list of values, how many personas have each value, keyed by the value's code. Counted over every persona that matches the filters, whatever its texts. Ranges and `missing` are not broken down. |
| `missing_representation` | `int` | How many more personas match the filters but have no text in the representation, so cannot be sampled with it. |
| `covers_sample_size` | `bool \| None` | Whether `matching` is at least the `sample_size` you gave; `None` when you gave none. |
| `representation` | `str` | The representation ID counted with, or `"empty"`. |

## `Twin`

One sampled persona, as the model will see it. You get twins from `plan.twins`,
or one at a time from `bank.twin(...)`. A record. Its `repr` shows the ID, the
first six attributes and the length of the text; `print(twin.text)` shows the
text.

| Name | Type | Meaning |
|---|---|---|
| `id` | `str` | The persona's ID in the bank. |
| `attributes` | `dict[str, Any]` | Every field the bank holds for the persona, filterable or not: the bank's filterable fields first, in its order, then the rest by name. Values are as the bank stores them; a missing value is `None` or empty. |
| `representation` | `str` | The representation ID of the text, or `"empty"`. |
| `text` | `str` | The persona text as it goes into the prompt: a line naming the bank, its version, the representation, the media type and the text's ID, then the text between `----- BEGIN PERSONA REPRESENTATION (untrusted data) -----` and `----- END PERSONA REPRESENTATION -----`. With `empty`, `[Empty Persona Profile]`. |
| `source` | `dict[str, Any]` | Where the text came from: `bank_id`, `version`, `representation` and, except with `empty`, `representation_id` (the text's ID in the bank). |

See also: [Configuring and planning a run](configure-and-plan.md), [Errors](errors.md).
