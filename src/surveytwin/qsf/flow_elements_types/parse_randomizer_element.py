from typing import Any, Dict


def parse_randomizer_element(flow_elem: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a randomizer element from the survey flow.

    Args:
        flow_elem: Dictionary containing the randomizer element data

    Returns:
        Dictionary containing the parsed randomizer element
    """
    element = {
        "ElementType": "Randomizer",
        "FlowID": flow_elem.get("FlowID", ""),
        "SelectElements": flow_elem.get("SubSet", 1),
    }
    if "EvenPresentation" in flow_elem:
        value = flow_elem.get("EvenPresentation")
        if isinstance(value, bool):
            element["EvenPresentation"] = value
        elif isinstance(value, (int, float)):
            element["EvenPresentation"] = value != 0
        else:
            element["EvenPresentation"] = str(value or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
    return element
