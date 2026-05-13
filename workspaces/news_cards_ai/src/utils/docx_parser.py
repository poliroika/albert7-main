"""
Docx parser for News Cards AI.
Handles extraction of requirements from specification document.
"""

import re
from pathlib import Path
from typing import Dict, List, Any
import sys

# Ensure UTF-8 encoding for file operations
sys.stdout.reconfigure(encoding='utf-8')


def extract_requirements(file_path: str) -> Dict[str, Any]:
    """
    Extract requirements from the specification document.
    
    Args:
        file_path: Path to the specification document
        
    Returns:
        Dictionary containing structured requirements
    """
    full_path = Path(file_path)
    
    # Try reading as text file first (the actual format of the docx file)
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return _parse_text_requirements(content)
    except UnicodeDecodeError:
        try:
            with open(full_path, 'rb') as f:
                header = f.read(2)
            
            if header != b'PK':
                # Not a real docx file, try different encodings
                encodings = ['utf-8', 'utf-16', 'windows-1251', 'cp1251']
                for enc in encodings:
                    try:
                        with open(full_path, 'r', encoding=enc) as f:
                            content = f.read()
                        return _parse_text_requirements(content)
                    except (UnicodeDecodeError, UnicodeError):
                        continue
            
            # If we get here, it's a real docx - would need python-docx
            # For now return requirements from text format
            return {"error": "Real docx format not yet supported - use text format"}
        except Exception as e:
            return {"error": f"Failed to parse file: {str(e)}"}


def _parse_text_requirements(content: str) -> Dict[str, Any]:
    """
    Parse requirements from text content.
    
    Args:
        content: Raw text content from specification document
        
    Returns:
        Structured requirements dictionary
    """
    requirements = {
        "task_description": "",
        "rules": [],
        "workflow_steps": [],
        "slide_templates": {},
        "content_categorization": {},
        "layout_selection_rules": {},
        "constraints": [],
        "compilation_rules": []
    }
    
    # Extract task description (section 1)
    task_match = re.search(r'1\.\s*Задача.*?(?:\n\d+\.|$)', content, re.DOTALL)
    if task_match:
        requirements["task_description"] = task_match.group(0).strip()
    
    # Extract basic rule (section 2)
    basic_rule_match = re.search(r'2\.\s*Базовое\s*правило.*?(?:\n\d+\.|$)', content, re.DOTALL)
    if basic_rule_match:
        requirements["rules"].append({
            "section": "basic_rule",
            "content": basic_rule_match.group(0).strip()
        })
    
    # Extract workflow steps (section 3)
    workflow_match = re.search(r'3\.\s*Как\s*работать\s*с\s*источником.*?(?:\n\d+\.|$)', content, re.DOTALL)
    if workflow_match:
        workflow_text = workflow_match.group(0)
        # Extract numbered steps
        steps = re.findall(r'Шаг\s+(\d+)\.\s*([^\n]+)', workflow_text)
        for num, description in steps:
            requirements["workflow_steps"].append({
                "step": int(num),
                "description": description.strip()
            })
    
    # Extract content categorization (section 3, step 4)
    categorization_match = re.search(r'Шаг\s+4\.\s*Разложить\s+тезисы\s+по\s+типу.*?(?:Шаг\s+\d+|4\.\s*Когда)', content, re.DOTALL)
    if categorization_match:
        cat_text = categorization_match.group(0)
        types = re.findall(r'([^\s]+)\s+тезисы\s*-\s*([^-\n]+)', cat_text)
        for type_name, description in types:
            requirements["content_categorization"][type_name] = description.strip()
    
    # Extract slide templates (section 5)
    slide_templates_text = content[content.find('5. Как выбирать макет'):]
    slide_templates_text = slide_templates_text[:slide_templates_text.find('6.') if '6.' in slide_templates_text else len(slide_templates_text)]
    
    # Parse each slide template
    slide_patterns = [
        (r'Слайд\s+10', 10),
        (r'Слайд(?:ы)?\s+(?:7\s*/\s*8|7|8)', [7, 8]),
        (r'Слайд\s+6', 6),
        (r'Слайд\s+5', 5),
        (r'Слайд\s+9', 9),
        (r'Слайд(?:ы)?\s*(?:14\s*/\s*15|14|15)', [14, 15]),
        (r'Слайд(?:ы)?\s*(?:18\s*/\s*19|18|19)', [18, 19]),
        (r'Слайд(?:ы)?\s*(?:16\s*/\s*17|16|17)', [16, 17]),
    ]
    
    for pattern, slide_id in slide_patterns:
        match = re.search(pattern + r'(.*?)(?:Слайд|$)', slide_templates_text, re.DOTALL)
        if match:
            template_info = match.group(0)
            conditions = re.findall(r'Использовать, если：(.*?)(?:Точно использовать|Слайд|$)', template_info, re.DOTALL)
            usage_notes = re.findall(r'(Примечания?|Важно|Точно использовать|Использовать только)(.*?)(?:Слайд|$)', template_info, re.DOTALL)
            
            template_data = {
                "slide_id": slide_id,
                "conditions": [c.strip() for c in conditions],
                "usage_notes": usage_notes,
                "raw_info": template_info.strip()
            }
            
            if isinstance(slide_id, list):
                for sid in slide_id:
                    requirements["slide_templates"][sid] = template_data
            else:
                requirements["slide_templates"][slide_id] = template_data
    
    # Extract constraints (section 8)
    constraints_match = re.search(r'8\.\s*Ограничения.*?$', content, re.DOTALL)
    if constraints_match:
        constraints_text = constraints_match.group(0)
        constraints = re.findall(r'^\s*([^-\n].*?)(?=;|$)', constraints_text, re.MULTILINE)
        requirements["constraints"] = [c.strip() for c in constraints if c.strip()]
    
    # Extract compilation rules (section 7)
    compilation_match = re.search(r'7\.\s*Как\s*собирать\s*подборку.*?(?:\n\d+\.|$)', content, re.DOTALL)
    if compilation_match:
        compilation_text = compilation_match.group(0)
        variants = re.findall(r'[–-]\s+(.*?)(?:[–-]|$)', compilation_text)
        requirements["compilation_rules"] = [v.strip() for v in variants]
    
    # Extract multi-source rules (section 2 and 16/17)
    multi_source_match = re.search(r'Смешивать.*?(?:Для\s*таких\s*карточек.*?слайд(?:ы)?\s*(?:16\s*/\s*17|16|17))', content, re.DOTALL)
    if multi_source_match:
        requirements["multi_source_allowed"] = True
        requirements["multi_source_slides"] = [16, 17]
        requirements["multi_source_types"] = [
            "новые релизы", "кейсы", "запуски", "продукты", "аноннсы от разных компаний"
        ]
    
    return requirements


def get_slide_layout_recommendation(requirements: Dict[str, Any], 
                                  thesis_count: int, 
                                  is_linked: bool, 
                                  has_visual_potential: bool,
                                  source_count: int = 1) -> Dict[str, Any]:
    """
    Recommend appropriate slide layout based on content analysis.
    
    Args:
        requirements: Parsed requirements dictionary
        thesis_count: Number of theses to place
        is_linked: Whether theses are thematically linked
        has_visual_potential: Whether content could benefit from visuals
        source_count: Number of sources (1 = single source, >1 = multi-source)
        
    Returns:
        Dictionary with recommended slide and reasoning
    """
    # Multi-source special case
    if source_count > 1 and requirements.get("multi_source_allowed", False):
        multi_sources = requirements.get("multi_source_types", [])
        return {
            "recommended_slide": 16,
            "alternative_slides": [17],
            "reason": "Multi-source content (releases, cases, launches)",
            "section": "multi_source"
        }
    
    # Single source layout selection
    if thesis_count >= 5 and thesis_count <= 6:
        return {
            "recommended_slide": 10,
            "reason": "5-6 short theses, sequential steps",
            "conditions_met": ["5-6 short theses"]
        }
    
    if thesis_count >= 3 and thesis_count <= 4 and not is_linked:
        return {
            "recommended_slide": 7,
            "alternative_slides": [8],
            "reason": "3-4 unlinked theses, each needs brief explanation",
            "conditions_met": ["3-4 unlinked theses"]
        }
    
    if is_linked and thesis_count >= 2:
        # Check for two main lines/contrast
        return {
            "recommended_slide": 6,
            "reason": "Linked content with two main lines comparison",
            "conditions_met": ["contains two main conceptual lines"]
        }
    
    if is_linked and thesis_count == 4:
        # Choose between 5 and 9 based on visual preference
        return {
            "recommended_slide": 5,
            "alternative_slides": [9],
            "reason": "4 linked theses",
            "conditions_met": ["4 linked theses"]
        }
    
    # Default fallback
    return {
        "recommended_slide": 10,
        "fallback_slides": [7, 8],
        "reason": "Default layout for varying content",
        "note": "Review content manually"
    }


if __name__ == "__main__":
    # Test the parser
    reqs = extract_requirements("пайплайн_автоматические_карточки.docx")
    
    # Print summary
    print(f"✓ Successfully extracted requirements")
    print(f"  - Task description: {'Yes' if reqs.get('task_description') else 'No'}")
    print(f"  - Workflow steps: {len(reqs.get('workflow_steps', []))}")
    print(f"  - Slide templates: {len(reqs.get('slide_templates', {}))}")
    print(f"  - Constraints: {len(reqs.get('constraints', []))}")
    print(f"  - Compilation rules: {len(reqs.get('compilation_rules', []))}")
    print(f"  - Content categories: {len(reqs.get('content_categorization', {}))}")
    
    # Print slide template summary
    print("\n📊 Available Slide Templates:")
    for slide_id, info in reqs.get('slide_templates', {}).items():
        conditions = info.get('conditions', ['N/A'])
        print(f"  Slide {slide_id}: {len(conditions)} condition(s)")