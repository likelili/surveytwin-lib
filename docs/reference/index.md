# surveytwin reference

This is the complete reference of the `surveytwin` library: every function,
class, method, setting and command, with what it takes, what it returns, what it
refuses, and a runnable example. For a walk-through from a survey file to
results, start with the [README](../../README.md); for writing and editing a
survey in Python, see [Editing a survey](survey.md#editing-a-survey); for the
draft builder a web form uses, [Building surveys without Qualtrics](../builder.md).

## A run, step by step

Five functions take a survey file to results. Each returns an object you can
print and inspect before going on, and nothing before `run` calls a model or
needs an API key.

```
read_survey("study.json")  ->  Survey  the survey: blocks, questions, flow, what is supported
read_qsf("study.qsf")  ->  Survey      the same, from a Qualtrics export
bank(...)              ->  Bank        the personas: representations, filters, sampling
configure(...)         ->  RunConfig   every setting of the run, checked
plan(survey, config)   ->  Plan        the sampled twins and each twin's survey; prompts to preview
run(plan)              ->  Results     every answer, the export files, the report, reruns
```

## Pages

| Page | What it covers |
|---|---|
| [The standard survey format](survey-format.md) | Writing a survey as JSON: questions, responses, conditions, blocks, flow, checks; how a QSF reads into it |
| [Reading, editing and checking a survey](survey.md) | `read_survey`, `read_qsf`, `new_survey`, `Survey` and what it reports: blocks, questions, embedded data, the check, dropping blocks; adding, changing, moving and removing questions and blocks |
| [Persona banks](personas.md) | `bank`, `Bank`, representations, filters, the audience preview, `Twin` |
| [Configuring and planning a run](configure-and-plan.md) | `configure`, `RunConfig`, `plan`, `Plan`, prompt previews |
| [Running a plan](running.md) | `run`, `arun`, API keys, static and staged runs, flagged answers, images, usage records, `surveytwin.batch` |
| [Models](models.md) | `Model` presets and defined models, `env`, `ModelRequest`, `Reply`, `ModelCheck` |
| [Results and export files](results.md) | `Results`, `Answer`, reruns, and every file in a run folder |
| [Prompt texts](prompts.md) | `surveytwin.prompts`: the texts every prompt is built from |
| [The draft builder](builder.md) | `surveytwin.builder`: a web form's drafts, building surveys from them and editing them |
| [Command line](command-line.md) | the `surveytwin` command and every option |
| [Errors](errors.md) | every error class and when it is raised |

## Every public name

| Name | Kind |
|---|---|
| [`surveytwin.read_survey`](survey.md#read_survey) | function |
| [`surveytwin.read_qsf`](survey.md#read_qsf) | function |
| [`surveytwin.new_survey`](survey.md#new_survey) | function |
| [`surveytwin.bank`](personas.md#bank) | function |
| [`surveytwin.configure`](configure-and-plan.md#configure) | function |
| [`surveytwin.plan`](configure-and-plan.md#plan) | function |
| [`surveytwin.run`](running.md#run) | function |
| [`surveytwin.arun`](running.md#arun) | async function |
| [`surveytwin.env`](models.md#env) | class |
| [`surveytwin.Model`](models.md#model) | class |
| [`surveytwin.ModelRequest`](models.md#modelrequest) | class |
| [`surveytwin.Reply`](models.md#reply) | class |
| [`surveytwin.ModelCheck`](models.md#modelcheck) | class |
| [`surveytwin.Survey`](survey.md#survey) | class |
| [`surveytwin.Question`](survey.md#question) | class |
| [`surveytwin.BlockPreview`](survey.md#blockpreview) | class |
| [`surveytwin.DropReport`](survey.md#dropreport) | class |
| [`surveytwin.SurveyCheck`](survey.md#surveycheck) | class |
| [`surveytwin.Finding`](survey.md#finding) | class |
| [`surveytwin.Bank`](personas.md#bank) | class |
| [`surveytwin.FilterOptions`](personas.md#filteroptions) | class |
| [`surveytwin.AudiencePreview`](personas.md#audiencepreview) | class |
| [`surveytwin.Twin`](personas.md#twin) | class |
| [`surveytwin.RunConfig`](configure-and-plan.md#runconfig) | class |
| [`surveytwin.Plan`](configure-and-plan.md#plan) | class |
| [`surveytwin.PromptPreview`](configure-and-plan.md#promptpreview) | class |
| [`surveytwin.Results`](results.md#results) | class |
| [`surveytwin.Answer`](results.md#answer) | class |
| [`surveytwin.SurveytwinError`](errors.md#surveytwinerror) | error |
| [`surveytwin.QSFReadError`](errors.md#qsfreaderror) | error |
| [`surveytwin.SurveyError`](errors.md#surveyerror) | error |
| [`surveytwin.BankError`](errors.md#bankerror) | error |
| [`surveytwin.PersonaError`](errors.md#personaerror) | error |
| [`surveytwin.FilterError`](errors.md#filtererror) | error |
| [`surveytwin.ConfigError`](errors.md#configerror) | error |
| [`surveytwin.StaticModeRefused`](errors.md#staticmoderefused) | error |
| [`surveytwin.BatchNotAvailable`](errors.md#batchnotavailable) | error |
| [`surveytwin.ContextBudgetError`](errors.md#contextbudgeterror) | error |
| [`surveytwin.ProviderError`](errors.md#providererror) | error |
| [`surveytwin.ImagesRefused`](errors.md#imagesrefused) | error |
| [`surveytwin.ResultsError`](errors.md#resultserror) | error |
| [`surveytwin.DraftError`](errors.md#drafterror) | error |
| [`surveytwin.SurveyFormatError`](errors.md#surveyformaterror) | error |
| [`surveytwin.UnsupportedEdit`](errors.md#unsupportededit) | error |
| [`surveytwin.builder.build_survey`](builder.md#build_survey) | function |
| [`surveytwin.builder.to_draft`](builder.md#to_draft) | function |
| [`surveytwin.builder.add_questions`](builder.md#add_questions) | function |
| [`surveytwin.builder.update_question`](builder.md#update_question) | function |
| [`surveytwin.builder.remove_questions`](builder.md#remove_questions) | function |
| [`surveytwin.builder.reorder_questions`](builder.md#reorder_questions) | function |
| [`surveytwin.builder.apply_draft`](builder.md#apply_draft) | function |
| [`surveytwin.builder.validate_draft`](builder.md#validate_draft) | function |
| [`surveytwin.builder.assign_ids`](builder.md#assign_ids) | function |
| [`surveytwin.builder.planned_qids`](builder.md#planned_qids) | function |
| [`surveytwin.builder.SurveyDraft`](builder.md#surveydraft) | class |
| [`surveytwin.builder.DraftQuestion`](builder.md#draftquestion) | class |
| [`surveytwin.builder.DraftIssue`](builder.md#draftissue) | class |
| [`surveytwin.builder.BuilderPolicy`](builder.md#builderpolicy) | class |
| [`surveytwin.builder.DEFAULT_POLICY`](builder.md#default_policy) | constant |
| [`surveytwin.builder.WEBSITE_POLICY`](builder.md#website_policy) | constant |
| [`surveytwin.builder.SCALE_PRESETS`](builder.md#scale_presets) | constant |
| [`surveytwin.builder.QUESTION_TYPES`](builder.md#question_types) | constant |
| [`surveytwin.builder.ANSWERABLE_TYPES`](builder.md#answerable_types) | constant |
| [`surveytwin.builder.DEFAULT_BLOCK_ID`](builder.md#default_block_id) | constant |
| [`surveytwin.builder.default_image_resolver`](builder.md#default_image_resolver) | function |
| [`surveytwin.batch.submit`](running.md#batchsubmit) | function |
| [`surveytwin.batch.status`](running.md#batchstatus) | function |
| [`surveytwin.batch.collect`](running.md#batchcollect) | function |
| [`surveytwin.prompts.PROMPT_VERSION`](prompts.md#prompt_version) | constant |
| [`surveytwin.prompts.DEFAULT_SYSTEM_INSTRUCTION`](prompts.md#default_system_instruction) | constant |
| [`surveytwin.prompts.SIMULATION_FORMAT_CONTRACT`](prompts.md#simulation_format_contract) | constant |
| [`surveytwin.prompts.RERUN_INSTRUCTION`](prompts.md#rerun_instruction) | constant |
| [`surveytwin.prompts.CT_SYSTEM_INSTRUCTION`](prompts.md#ct_system_instruction) | constant |

Classes the public objects return but that are not exported themselves (such as
`FilterField`, `Representation`, `EmbeddedField` and `Dependency`) are documented
on the page of the object that holds them.
