class AgentError(Exception):
    """Safe, user-facing agent failure."""


class UnknownToolError(AgentError):
    pass


class ToolValidationError(AgentError):
    pass


class ClarificationRequired(AgentError):
    pass


