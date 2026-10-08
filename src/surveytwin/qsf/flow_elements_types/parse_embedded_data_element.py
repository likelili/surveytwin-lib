import copy
from typing import Any, Dict


def parse_embedded_data_element(flow_elem: Dict[str, Any]) -> Dict[str, Any]:
    """Parse an embedded data element from the survey flow.

    ``EmbeddedData`` holds the rows that set a value, in order: ``Field`` and
    ``Value``, which the runtime reads, plus the rest of the row as exported
    (``Type``, ``VariableType``, ...). A row with no value that the study takes
    from a panel, contact list or the survey link (``Type: Recipient``) sets
    nothing: it is kept in ``Declared``, so the survey definition still says the
    field comes from outside.

    Args:
        flow_elem: Dictionary containing the embedded data element data

    Returns:
        Dictionary containing the parsed embedded data element
    """
    element: Dict[str, Any] = {
        "ElementType": "EmbeddedData",
        "FlowID": flow_elem.get("FlowID", ""),
        "EmbeddedData": [],
    }
    declared = []

    for data in flow_elem.get("EmbeddedData") or []:
        if not isinstance(data, dict):
            continue
        if data.get("Type") == "Recipient":
            declared.append({"Field": data.get("Field", ""), **{k: copy.deepcopy(v) for k, v in data.items() if k not in {"Field", "Value"}}})
            continue
        row = {"Field": data.get("Field", ""), "Value": data.get("Value", "")}
        row.update({k: copy.deepcopy(v) for k, v in data.items() if k not in row})
        element["EmbeddedData"].append(row)

    if declared:
        element["Declared"] = declared
    return element
