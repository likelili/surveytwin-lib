"""surveytwin: run a Qualtrics survey on digital twins and get Qualtrics-format data back.

    import surveytwin as st
    survey = st.read_qsf("study.qsf")
    config = st.configure(sample_size=50, seed=7, model="gpt-5.6-luna-medium")
    plan = st.plan(survey, config)
    print(plan.preview_prompt())
    results = st.run(plan)
    results.save("runs/study")
"""

from ._version import __version__
from . import batch, builder, prompts
from .config import RunConfig, configure
from .errors import (
    BankError,
    BatchNotAvailable,
    ConfigError,
    ContextBudgetError,
    DraftError,
    FilterError,
    ImagesRefused,
    PersonaError,
    ProviderError,
    QSFReadError,
    ResultsError,
    StaticModeRefused,
    SurveyError,
    SurveyFormatError,
    SurveytwinError,
    UnsupportedEdit,
)
from .personas.bank import AudiencePreview, Bank, FilterOptions, Twin, open_bank as bank
from .plan import Plan, PromptPreview, plan
from .providers.models import Model, ModelCheck, ModelRequest, Reply, env
from .results.results import Answer, Results
from .run import arun, run
from .survey import BlockPreview, DropReport, Finding, Question, Survey, SurveyCheck, read_qsf, read_survey
from .builder.standard import new_survey

__all__ = [
    "__version__",
    "read_qsf",
    "read_survey",
    "new_survey",
    "bank",
    "configure",
    "plan",
    "run",
    "arun",
    "batch",
    "builder",
    "prompts",
    "env",
    "Model",
    "ModelRequest",
    "Reply",
    "ModelCheck",
    "Survey",
    "Question",
    "BlockPreview",
    "DropReport",
    "SurveyCheck",
    "Finding",
    "Bank",
    "FilterOptions",
    "AudiencePreview",
    "Twin",
    "RunConfig",
    "Plan",
    "PromptPreview",
    "Results",
    "Answer",
    "SurveytwinError",
    "QSFReadError",
    "SurveyError",
    "SurveyFormatError",
    "BankError",
    "PersonaError",
    "FilterError",
    "ConfigError",
    "StaticModeRefused",
    "BatchNotAvailable",
    "ContextBudgetError",
    "ProviderError",
    "ImagesRefused",
    "ResultsError",
    "DraftError",
    "UnsupportedEdit",
]
