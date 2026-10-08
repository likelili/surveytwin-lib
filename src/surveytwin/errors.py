"""Every error the library raises shares one base class, ``SurveytwinError``.

Messages name the survey element, twin or file involved.
"""

from __future__ import annotations


class SurveytwinError(Exception):
    """Base class for all library errors."""


class QSFReadError(SurveytwinError):
    """The file is not a readable QSF (not JSON, or no SurveyElements or flow)."""


class SurveyError(SurveytwinError):
    """A problem with the survey, such as an unknown block ID or an unresolved dependency."""


class BankError(SurveytwinError):
    """A persona bank could not be read, or failed the Croissant Twins checks."""


class PersonaError(SurveytwinError):
    """A twin cannot be used, such as a blank persona."""


class FilterError(SurveytwinError):
    """A filter names an unknown field or value."""


class ConfigError(SurveytwinError):
    """An invalid run setting or model definition."""


class StaticModeRefused(SurveytwinError):
    """Static mode was asked for a survey that needs staged execution."""


class BatchNotAvailable(SurveytwinError):
    """Batch was asked for where it cannot work (staged survey, non-OpenAI model)."""


class ContextBudgetError(SurveytwinError):
    """A prompt would exceed the model's context; nothing is cut to fit."""


class ProviderError(SurveytwinError):
    """A model call failed after retries, or a model check failed."""


class ImagesRefused(SurveytwinError):
    """The survey has images the model cannot receive or that are unreachable."""


class ResultsError(SurveytwinError):
    """A saved run folder is missing files or fails its checksums."""


class DraftError(SurveyError):
    """A survey draft or a builder edit has errors. ``issues`` lists every one,
    each tied to a question (ID and position) and a field, for a web form."""

    def __init__(self, issues: list, message: str | None = None) -> None:
        self.issues = list(issues)
        errors = [i for i in self.issues if getattr(i, "severity", "error") == "error"]
        if message is None:
            shown = "; ".join(str(i) for i in errors[:5])
            more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
            message = f"{len(errors)} problem{'s' if len(errors) != 1 else ''}: {shown}{more}"
        super().__init__(message)


class SurveyFormatError(SurveyError):
    """A survey in the standard format has errors. ``findings`` lists every
    problem with the path of its field (``$.questions[2].response.options``)."""

    def __init__(self, findings: list, message: str | None = None) -> None:
        self.findings = list(findings)
        errors = [f for f in self.findings if getattr(f, "level", "error") == "error"]
        if message is None:
            shown = "; ".join(f"{f.path}: {f.message}" for f in errors[:5])
            more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
            message = f"{len(errors)} problem{'s' if len(errors) != 1 else ''}: {shown}{more}"
        super().__init__(message)


class UnsupportedEdit(SurveyError):
    """A builder edit that would lose survey content or break the survey's logic;
    nothing was changed."""

