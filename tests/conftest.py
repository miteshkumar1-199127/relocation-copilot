import os

# Unit tests use fake providers and should never emit LangSmith traces.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
