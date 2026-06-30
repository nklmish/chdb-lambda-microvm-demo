import os
import logging
from memory import AnalyticalMemory

logger = logging.getLogger(__name__)


def create_strands_session_manager(user_id: str, session_id: str):
    """Create an AgentCoreMemorySessionManager for Strands Agent integration.
    
    Returns None if AGENTCORE_MEMORY_ID is not set (local dev mode).
    Pass the result to Agent(session_manager=...).
    """
    memory_id = os.getenv("AGENTCORE_MEMORY_ID")
    if not memory_id:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
            RetrievalConfig,
        )
        
        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=user_id,
            retrieval_config={
                # Semantic facts discovered from analysis
                f"taxi-analytics/{user_id}/": RetrievalConfig(top_k=10, relevance_score=0.3),
                # User preferences for analysis style
                f"taxi-analytics/{user_id}/preferences/": RetrievalConfig(top_k=5, relevance_score=0.5),
                # Cross-user insights from episodic reflection
                "taxi-analytics/insights/": RetrievalConfig(top_k=3, relevance_score=0.7),
            },
            batch_size=1,  # Immediate send — no buffering needed for request/response
        )
        return AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
    except Exception as e:
        logger.warning("Failed to create AgentCore Memory session manager: %s", e)
        return None


class DualMemory:
    """Coordinates chDB session memory + AgentCore persistent memory.
    
    AgentCore Memory is handled AUTOMATICALLY by the Strands session_manager —
    the agent saves/retrieves persistent memories as part of its invocation loop.
    This class only manages chDB (local, fast) operations explicitly.
    """
    
    def __init__(self, session_memory: AnalyticalMemory, user_id: str, session_id: str = "default"):
        self.session = session_memory          # chDB (fast, ephemeral)
        self.user_id = user_id
        self._session_id = session_id
        self.strands_session_manager = create_strands_session_manager(user_id, session_id)
    
    def get_local_context(self, limit: int = 5) -> str:
        """Get recent context from chDB only (fast, no network).
        
        AgentCore persistent context is injected automatically by the
        session_manager during agent invocation — do NOT retrieve it here
        to avoid double-injection.
        """
        return self.session.get_recent_context(limit)
    
    def save_conversation(self, role: str, content: str) -> None:
        """Save to chDB session memory. AgentCore save is automatic via session_manager."""
        self.session.save_conversation(role, content)
    
    def save_analysis(self, description: str, params: dict, 
                      result_summary: str, execution_ms: int) -> None:
        """Save analysis to chDB with structured fields."""
        self.session.save_analysis(description, params, result_summary, execution_ms)
