"""
NationClaw - Observability & Trust (FULL Working Implementation)
"""
import json
import logging
import time
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import os

logger = logging.getLogger(__name__)

class EventType(Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    ERROR = "error"

class TrustLevel(Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"

@dataclass
class AuditEvent:
    timestamp: float
    event_type: EventType
    description: str
    agent_id: str
    session_id: str
    trust_level: TrustLevel = TrustLevel.SAFE
    risk_score: float = 0.0
    
    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "event_type": self.event_type.value,
            "description": self.description,
            "trust_level": self.trust_level.value,
            "risk_score": self.risk_score
        }

class AuditLogger:
    def __init__(self, log_dir="./logs", agent_id="agent", session_id=None):
        self.log_dir = log_dir
        self.agent_id = agent_id
        self.session_id = session_id or f"session_{int(time.time())}"
        self.events = []
        self.event_count = 0
        
        os.makedirs(log_dir, exist_ok=True)
        self.log_file = os.path.join(log_dir, f"audit_{self.session_id}.jsonl")
        logger.info(f"AuditLogger initialized: {self.session_id}")
    
    def log_event(self, event_type, description, **kwargs):
        event = AuditEvent(
            timestamp=time.time(),
            event_type=event_type,
            description=description,
            agent_id=self.agent_id,
            session_id=self.session_id,
            **kwargs
        )
        
        self.events.append(event)
        self.event_count += 1
        
        try:
            with open(self.log_file, 'a') as f:
                f.write(json.dumps(event.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to write event: {e}")
        
        log_msg = f"[{event.event_type.value}] {event.description}"
        if event.trust_level == TrustLevel.DANGEROUS:
            logger.warning(log_msg)
        else:
            logger.info(log_msg)
        
        return event
    
    def get_statistics(self):
        return {"total_events": len(self.events)}

class SafetyChecker:
    def __init__(self):
        self.risk_rules = {
            "install_app": {"base_risk": 0.3},
            "delete_file": {"base_risk": 0.5}
        }
        self.app_blacklist = set()
        logger.info("SafetyChecker initialized")
    
    def assess_risk(self, action, parameters, context):
        risk_score = self.risk_rules.get(action, {}).get("base_risk", 0.0)
        
        pkg = parameters.get("package_name")
        if pkg and pkg in self.app_blacklist:
            risk_score += 1.0
        
        if risk_score >= 0.8:
            return TrustLevel.DANGEROUS, risk_score, []
        return TrustLevel.SAFE, risk_score, []
    
    def add_to_blacklist(self, app_package):
        self.app_blacklist.add(app_package)

class TrustScorer:
    def __init__(self):
        self.action_history = {}
        self.trust_scores = {}
        logger.info("TrustScorer initialized")
    
    def record_action(self, action, success):
        if action not in self.action_history:
            self.action_history[action] = []
        self.action_history[action].append(success)
        self._update_trust_score(action)
    
    def get_trust_score(self, action):
        return self.trust_scores.get(action, 0.5)
    
    def _update_trust_score(self, action):
        history = self.action_history.get(action, [])
        if not history:
            self.trust_scores[action] = 0.5
            return
        
        success_rate = sum(1 for h in history if h) / len(history)
        self.trust_scores[action] = success_rate
AUDITEOF`
echo "✓ Created nationclaw/observability/audit.py"
