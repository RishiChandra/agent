import os
from openai import AzureOpenAI
from openai import APIError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Azure OpenAI Configuration
deployment = "gpt-4.1-nano"
api_version = "2024-12-01-preview"
endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
api_key = os.environ["AZURE_OPENAI_API_KEY"]

def call_openai(messages, tools=None):
    client = get_openai_client()
    deployment_name = get_deployment_name()

    if tools is not None:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=messages,
            tools=tools,
            tool_choice="required"
        )
    else:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=messages
        )
    
    return response

def get_openai_client():
    """
    Get a configured Azure OpenAI client instance.
    
    Returns:
        AzureOpenAI: Configured client instance or None if configuration is missing
        
    Raises:
        ValueError: If required environment variables are not set
    """
    if not endpoint or not api_key:
        raise ValueError("Missing AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_API_KEY environment variables")
    
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version
    )

def get_deployment_name():
    return deployment

def get_api_version():
    return api_version