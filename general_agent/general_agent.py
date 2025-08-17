import logging
import asyncio

from openai_client import get_openai_client, get_deployment_name

def call_openai(prompt):
    try:
        client = get_openai_client()
        deployment = get_deployment_name()
        
        response = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return response.choices[0].message.content
    except ValueError as e:
        print(f"Configuration Error: {e}")
        return None
    except Exception as e:
        print(f"Error calling OpenAI: {e}")
        return None