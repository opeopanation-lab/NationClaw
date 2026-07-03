"""
NationClaw - Skill Studio (FULL Working Implementation)
"""
import json
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import os

logger = logging.getLogger(__name__)

class SkillType(Enum):
    TASK_AUTOMATION = "task_automation"
    APP_INTERACTION = "app_interaction"

@dataclass
class SkillStep:
    step_id: str
    description: str
    action: str
    parameters: Dict[str, Any]
    fallback_steps: List[str] = field(default_factory=list)

@dataclass
class Skill:
    name: str
    display_name: str
    description: str
    version: str
    steps: List[SkillStep]
    tags: List[str] = field(default_factory=list)
    usage_count: int = 0
    success_rate: float = 0.0
    
    def validate(self):
        if not self.name or not self.steps:
            return {"valid": False, "errors": ["Invalid skill"]}
        return {"valid": True, "errors": []}

class SkillStudio:
    def __init__(self, skills_dir="./skills"):
        self.skills_dir = skills_dir
        self.skills = {}
        os.makedirs(skills_dir, exist_ok=True)
        logger.info("SkillStudio initialized")
    
    def create_skill(self, skill):
        if skill.name in self.skills:
            return False
        self.skills[skill.name] = skill
        self._save_skill(skill)
        logger.info(f"Created skill: {skill.name}")
        return True
    
    def execute_skill(self, skill_name, context, tool_registry):
        skill = self.get_skill(skill_name)
        if not skill:
            raise ValueError(f"Skill '{skill_name}' not found")
        
        results = []
        for step in skill.steps:
            try:
                result = tool_registry.execute_tool(step.action, step.parameters)
                results.append({"step": step.step_id, "success": True})
            except Exception as e:
                logger.error(f"Step {step.step_id} failed: {e}")
                results.append({"step": step.step_id, "success": False})
        
        skill.usage_count += 1
        successful = sum(1 for r in results if r["success"])
        skill.success_rate = (skill.success_rate * (skill.usage_count - 1) + 
                             successful / len(results)) / skill.usage_count
        
        self._save_skill(skill)
        return {"skill": skill_name, "results": results}
    
    def get_skill(self, name):
        return self.skills.get(name)
    
    def list_skills(self):
        return list(self.skills.keys())
    
    def _save_skill(self, skill):
        filepath = os.path.join(self.skills_dir, f"{skill.name}.json")
        with open(filepath, 'w') as f:
            json.dump({
                "name": skill.name,
                "display_name": skill.display_name,
                "steps": [{"step_id": s.step_id, "action": s.action, "parameters": s.parameters} 
                          for s in skill.steps]
            }, f, indent=2)
