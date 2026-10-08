import copy
import html
import json
import logging
import os
import sys
from typing import Any, Dict, List

from .flow_elements_types import (
    parse_branch_element,
    parse_embedded_data_element,
    parse_group_element,
    parse_randomizer_element,
)
from .question_types import (
    extract_choice_question_data,
    extract_constant_sum_question_data,
    extract_matrix_question_data,
    extract_rank_question_data,
    extract_sbs_question_data,
    extract_slider_question_data,
    extract_text_question_data,
)
from .html_text import html_text
from .question_types.base import extract_image_media
from .question_types.common import apply_common_question_features
from ..runtime.choice_logic import item_logic

logger = logging.getLogger(__name__)

METADATA_FIELDS = [
    "SurveyID",
    "SurveyName",
    "SurveyDescription",
    "SurveyOwnerID",
    "SurveyBrandID",
    "SurveyLanguage",
    "SurveyCreationDate",
    "LastModified",
    "LastActivated",
]


def parse_qsf_file(qsf_file_path: str, exclude_questions: List[str] = None) -> Dict[str, Any]:
    """Parse a QSF file and extract all questions with their metadata
    in a structured format
    """
    with open(qsf_file_path, "r", encoding="utf-8") as f:
        qsf_data = json.load(f)
    return parse_qsf_data(qsf_data, exclude_questions)


def parse_qsf_data(qsf_data: Dict[str, Any], exclude_questions: List[str] = None) -> Dict[str, Any]:
    """Parse already-loaded QSF JSON; the same result as ``parse_qsf_file``."""
    # Handle None for exclude_questions
    if exclude_questions is None:
        exclude_questions = []

    # Extract top-level metadata if available
    metadata = {}
    if "SurveyEntry" in qsf_data:
        metadata = qsf_data.get("SurveyEntry", {})
        ## keep only the fields in METADATA_FIELDS
        metadata = {k: v for k, v in metadata.items() if k in METADATA_FIELDS}
    survey_title = metadata.get("SurveyName", "Untitled Survey")

    # Extract survey elements and metadata
    survey_elements = qsf_data.get("SurveyElements", [])

    # Extract survey flow information for randomizers and blocks order
    flow_element = None
    for elem in survey_elements:
        if elem and elem.get("Element") == "FL":
            flow_element = elem.get("Payload", {})
            break

    survey_flow = {}
    if flow_element:
        try:
            # Extract flow structure - this helps with randomizers
            survey_flow = {
                "flow_id": flow_element.get("FlowID", ""),
                "flow_type": flow_element.get("Type", ""),
                "flow_elements": flow_element.get("Flow", []),
            }
        except Exception as e:
            logger.warning(f"Error extracting survey flow: {e}")
            logger.debug("Traceback for error extracting survey flow:", exc_info=True)

    logger.info(f"Parsing survey: {survey_title}")

    # Get question elements (those with Element="SQ")
    question_elements = [elem for elem in survey_elements if elem and elem.get("Element") == "SQ"]
    logger.info(f"Found {len(question_elements)} question elements in the QSF file")

    # Extract web service elements with ContentType
    web_service_elements = extract_web_service_elements(survey_elements)

    # Extract question blocks and their organization
    block_elements = [elem for elem in survey_elements if elem and elem.get("Element") == "BL"]

    # Map of block ID to block name/type
    all_blocks = {}
    # Map of question ID to block ID
    question_to_block = {}
    # Identify trash questions to skip
    trash_question_ids = []

    for block in block_elements:
        if block and "Payload" in block:
            payload = block.get("Payload")

            # The payload can be a dictionary or a list
            block_data_iterator = []
            if isinstance(payload, dict):
                block_data_iterator = payload.values()
            elif isinstance(payload, list):
                block_data_iterator = payload

            for block_data in block_data_iterator:
                if isinstance(block_data, dict):
                    block_name = block_data.get("Description", "")
                    block_type = block_data.get("Type", "")
                    block_ID = block_data.get("ID", "")
                    block_options = block_data.get("Options", {})
                    all_blocks[block_ID] = {
                        "name": block_name,
                        "type": block_type,
                        "block_ID": block_ID,
                        "block_options": block_options,
                        "block_elements": [],
                        "block_element_metadata": {},
                    }

                    # Mark trash questions
                    if block_type == "Trash":
                        for elem in block_data.get("BlockElements", []):
                            if elem and elem.get("Type") == "Question":
                                q_id = elem.get("QuestionID")
                                if q_id:
                                    trash_question_ids.append(q_id)

                    # Map questions to their blocks
                    for elem in block_data.get("BlockElements", []):
                        if elem and elem.get("Type") == "Question":
                            q_id = elem.get("QuestionID")
                            if q_id in exclude_questions:
                                continue
                            if q_id:
                                question_to_block[q_id] = block_ID
                                all_blocks[block_ID]["block_elements"].append(q_id)
                                all_blocks[block_ID]["block_element_metadata"][q_id] = copy.deepcopy(
                                    elem
                                )
    logger.info(f"Identified {len(trash_question_ids)} trash questions to skip")

    # Extract all questions with desired format
    all_questions = {}

    for elem in question_elements:
        try:
            if not elem or not elem.get("Payload"):
                continue

            payload = elem.get("Payload", {})
            primary_attr = elem.get("PrimaryAttribute")

            if primary_attr and primary_attr in trash_question_ids:
                continue

            question_id = payload.get("QuestionID", "")
            question_type = payload.get("QuestionType", "")

            if question_id in exclude_questions:
                continue

            if not question_id or not question_type:
                continue

            all_questions[question_id] = (payload, question_id)
        except Exception as e:
            logger.warning(f"Error processing question element: {e}")
            logger.debug("Traceback for error processing question element:", exc_info=True)
            continue

    # Organize results
    result = {
        "survey_title": survey_title,
        "metadata": metadata,
        "survey_metadata": metadata,
        "all_questions": all_questions,
        "survey_flow": survey_flow,
        "all_blocks": all_blocks,
        "web_services": web_service_elements,
    }
    return result


def extract_question_data(
    payload: Dict[str, Any],
    question_id: str,
    survey_brand_id: str = "",
) -> Dict[str, Any]:
    """Extract all relevant data for a question from its payload"""
    question_name = payload.get("DataExportTag", "")
    raw_question_text = payload.get("QuestionText", "").strip()
    question_text = raw_question_text
    raw_question_type = str(payload.get("QuestionType", "") or "").strip()
    question_type = "Rank" if raw_question_type.upper() == "RO" else raw_question_type

    logger.info(f"Processing question ID: {question_id}, type: {raw_question_type}")

    # Clean HTML tags from question text: laid out in lines as a browser shows it
    # (entities resolved first, as before), a table with every row and cell.
    keep_lines = True
    question_text = html_text(html.unescape(question_text))
    question_media = extract_image_media(payload, survey_brand_id=survey_brand_id)
    if not question_text and question_media:
        question_text = "[Image stimulus attached]"

    # Create base question data
    question_data = {
        "QuestionID": question_id,
        "QuestionText": question_text,
        "QuestionType": question_type,
        "QuestionName": question_name,
    }
    if raw_question_type != question_type:
        question_data["RawQuestionType"] = raw_question_type
    if keep_lines:
        question_data["KeepLines"] = True
    if question_media:
        question_data["QuestionMedia"] = question_media
    if "DisplayLogic" in payload:
        question_data["DisplayLogic"] = payload["DisplayLogic"]

    try:
        # Handle different question types
        if question_type == "Matrix":
            # Matrix question handling
            question_data.update(extract_matrix_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "MC":
            # Multiple choice question handling
            question_data.update(extract_choice_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "TE":
            # Text entry question handling
            question_data.update(extract_text_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "DB":
            # Descriptive text block (not a question)
            question_data["is_descriptive"] = True
            question_data["Settings"] = {
                "Selector": payload.get("Selector", ""),
                "SubSelector": payload.get("SubSelector", ""),
            }
        elif question_type == "Slider":
            # Slider question
            question_data.update(extract_slider_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "CS":
            # Constant sum question
            question_data.update(extract_constant_sum_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "Rank":
            # Ranking question
            question_data.update(extract_rank_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "SBS":
            # Side by side questions
            question_data.update(extract_sbs_question_data(payload, survey_brand_id=survey_brand_id))
        elif question_type == "Captcha":
            # Captcha question
            question_data["is_captcha"] = True
        elif question_type == "Timing":
            # Timing question
            question_data["is_timing"] = True
        elif question_type == "Meta":
            question_data["is_meta"] = True
        else:
            # Unsupported Qualtrics question type (e.g. NPS, Calendar, HeatMap,
            # Signature). Keep an inert record so upload previews and the
            # validation report can warn about it, but never pose it to a twin
            # (instrument_builder skips is_unsupported questions).
            question_data["is_unsupported"] = True
            question_data["Settings"] = {
                "Selector": payload.get("Selector", ""),
                "SubSelector": payload.get("SubSelector", ""),
            }

        # Type-agnostic constraints & options: validation superset,
        # randomization, recode preservation, normalized defaults. Additive -
        # per-type extractor keys always win.
        apply_common_question_features(question_data, payload, question_type)
        # Display logic on single options, statements and scale points, by item
        # ID; the runtimes apply it.
        question_data.update(item_logic(payload))

        if "RecodeValues" in payload and question_type not in ["MC", "Matrix", "Rank", "CS"]:
            # Preserve unexecuted recodes instead of failing the whole parse;
            # exports fall back to the original choice IDs for this question.
            logger.warning(
                "RecodeValues on %s question %s is preserved but not applied to export values.",
                question_type,
                question_id,
            )
            question_data["RecodeValuesUnsupported"] = True
    except Exception as e:
        raise ValueError(
            f"Error extracting data for question {question_id} of type {question_type}: {e}"
        )
        # logger.warning(f"Error extracting data for question {question_id} of type {question_type}: {e}")
        # logger.debug(f"Traceback for error extracting data for question {question_id}:", exc_info=True)

    return question_data


def save_output(output_data: Dict[str, Any], output_file_path: str) -> None:
    """Save the parsed data to a JSON file"""
    with open(output_file_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4)


def generate_template_format(
    parsed_data: Dict[str, Any], exclude_flow_elements: List[str] = None
) -> Dict[str, Any]:
    """Convert the parsed survey data to the format used in wave4_survey_template.json"""
    # Handle None for exclude_flow_elements
    if exclude_flow_elements is None:
        exclude_flow_elements = []
    
    # Extract metadata
    metadata = {}
    survey_metadata = parsed_data.get("survey_metadata") or parsed_data.get("metadata") or {}
    if isinstance(survey_metadata, dict):
        sm = survey_metadata
        metadata = {
            "SurveyID": sm.get("SurveyID", ""),
            "SurveyName": sm.get("SurveyName", parsed_data.get("survey_title", "Untitled Survey")),
            "SurveyDescription": sm.get("SurveyDescription"),
            "SurveyOwnerID": sm.get("SurveyOwnerID", ""),
            "SurveyCreationDate": sm.get("SurveyCreationDate", ""),
            "SurveyLastModified": sm.get("SurveyLastModified", sm.get("LastModified", "")),
            "SurveyBrandID": sm.get("SurveyBrandID", ""),
            "SurveyLanguage": sm.get("SurveyLanguage", ""),
            "SurveyStatus": sm.get("SurveyStatus", ""),
        }

        # Remove any empty or None values from metadata
        metadata = {k: v for k, v in metadata.items() if v is not None and v != ""}

    survey_brand_id = str(metadata.get("SurveyBrandID", "") or "")

    # Process all blocks into their own elements
    block_elements = {}
    for block_ID, block_data in parsed_data.get("all_blocks", {}).items():
        # Skip empty blocks or blocks with no name
        if not block_data.get("block_elements", []):
            continue

        block_name = block_data.get("name", "Unknown Block")
        block_type = block_data.get("type", "Unknown Block")
        block_options = block_data.get("block_options", {})
        if block_type == "Trash":
            continue
        questions_ids = block_data.get("block_elements", [])
        questions = [
            parsed_data.get("all_questions", {})[question_id] for question_id in questions_ids
        ]

        block_element = {
            "ElementType": "Block",
            "BlockName": block_name,
            "BlockType": block_type,
            "BlockID": block_ID,
            "Questions": [],
        }
        if (
            "Looping" in block_options
            or "Randomization" in block_options
            or "RandomizeQuestions" in block_options
        ):
            block_element["Options"] = block_options

        for question in questions:
            # Skip certain question types
            # if q_type == "DB":  # Skip descriptive blocks
            #    continue
            block_element["Questions"].append(question)

        # Only add blocks with questions
        if block_element["Questions"]:
            block_elements[block_ID] = block_element

    def parse_flow_element(flow_elem):
        """Recursively parse the elements in the flow"""
        element = {}

        if flow_elem.get("FlowID") in exclude_flow_elements:
            return None

        if flow_elem.get("Type") == "EmbeddedData":
            element = parse_embedded_data_element(flow_elem)

        if flow_elem.get("Type") == "Branch":
            element = parse_branch_element(flow_elem)

        if flow_elem.get("Type") == "BlockRandomizer":
            element = parse_randomizer_element(flow_elem)

        if flow_elem.get("Type") == "Group":
            element = parse_group_element(flow_elem)

        if flow_elem.get("Type") == "EndSurvey":
            element = {
                "ElementType": "EndSurvey",
                "FlowID": flow_elem.get("FlowID", ""),
                "Options": flow_elem.get("Options", {}),
            }

        if flow_elem.get("Flow"):
            element["Elements"] = []
            for sub_elem in flow_elem.get("Flow"):
                parsed_elem = parse_flow_element(sub_elem)
                if parsed_elem:
                    element["Elements"].append(parsed_elem)

        if flow_elem.get("Type") == "Standard" or flow_elem.get("Type") == "Block":
            logger.info(f"Processing Block: {flow_elem.get('ID')}")

            if flow_elem.get("ID") in block_elements:
                block_element = copy.deepcopy(block_elements[flow_elem.get("ID")])
                block_metadata = parsed_data.get("all_blocks", {}).get(
                    flow_elem.get("ID"), {}
                ).get("block_element_metadata", {})
                extracted_questions = []
                for payload, question_id in block_element["Questions"]:
                    question_data = extract_question_data(
                        payload,
                        question_id,
                        survey_brand_id=survey_brand_id,
                    )
                    block_question_metadata = block_metadata.get(question_id, {})
                    # Qualtrics stores SkipLogic on BL BlockElements, not in the SQ payload.
                    skip_logic = block_question_metadata.get("SkipLogic")
                    if isinstance(skip_logic, list) and skip_logic:
                        question_data["SkipLogic"] = copy.deepcopy(skip_logic)
                    extracted_questions.append(question_data)
                block_element["Questions"] = extracted_questions
                return block_element
            else:
                ## the block is filtered out
                return None

        return element

    ### parse survey flow
    elements = []
    for elem in parsed_data.get("survey_flow", {}).get("flow_elements", []):
        parsed_elem = parse_flow_element(elem)
        if parsed_elem:
            elements.append(parsed_elem)

    # Add WebService elements if available
    web_services = []
    for ws in parsed_data.get("web_services", []):
        if ws.get("ContentType") == "application/json":
            web_service = {
                "ElementType": "WebService",
                "Path": ws.get("Path", ""),
                "Method": ws.get("Method", "POST"),
                "ContentType": "application/json",
                "Body": ws.get("Body", {}),
            }
            web_services.append(web_service)

    # Create the final output structure
    result = {"Metadata": metadata, "Elements": elements}

    # Add WebServices if any were found
    if web_services:
        result["WebServices"] = web_services

    return result


def extract_web_service_elements(survey_elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract WebService elements from the survey that might have ContentType fields"""
    web_service_elements = []

    # Look for the Survey Flow element that contains WebService definitions
    for elem in survey_elements:
        if elem.get("Element") == "FL" and elem.get("PrimaryAttribute") == "Survey Flow":
            flow_payload = elem.get("Payload", {})
            if "Flow" in flow_payload:
                # Extract all WebService elements from the flow
                extract_web_services_from_flow(flow_payload.get("Flow", []), web_service_elements)

    logger.info(f"Found {len(web_service_elements)} WebService elements")
    return web_service_elements


def extract_web_services_from_flow(flow: List[Dict[str, Any]], results: List[Dict[str, Any]]):
    """Recursively extract WebService elements from a survey flow"""
    for item in flow:
        if not isinstance(item, dict):
            continue

        item_type = item.get("Type")

        # If this is a WebService element, add it to results
        if item_type == "WebService":
            # Check if it has ContentType set to application/json
            if item.get("ContentType") == "application/json":
                logger.info(
                    f"Found WebService with application/json ContentType: {item.get('Path', 'unknown')}"
                )
                results.append(item)

        # Recursively process nested flows (in branches, etc.)
        if "Flow" in item and isinstance(item["Flow"], list):
            extract_web_services_from_flow(item["Flow"], results)
