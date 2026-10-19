from . import analysis, execution, files_knowledge, web


def register_all(reg, app) -> None:
    files_knowledge.register(reg)
    analysis.register(reg)
    execution.register(reg, app)
    web.register(reg)
