"""utils/langfuse_utils.py — Initialize Langfuse client from SSM credentials."""
import os
from utils.aws import get_ssm_parameter

def get_langfuse_client():
    """Fetch Langfuse credentials from SSM and return initialized client."""
    from langfuse import get_client
    
    os.environ["LANGFUSE_SECRET_KEY"] = get_ssm_parameter("/langfuse/LANGFUSE_SECRET_KEY")
    os.environ["LANGFUSE_PUBLIC_KEY"] = get_ssm_parameter("/langfuse/LANGFUSE_PUBLIC_KEY")
    os.environ["LANGFUSE_HOST"] = get_ssm_parameter("/langfuse/LANGFUSE_HOST")
    
    return get_client()
