from __future__ import annotations


_GREETINGS = {
    "zh": "NotePatch AI 可以帮助整理思路、分析学习资料并回答问题，回复支持 Markdown。",
    "pt": "A NotePatch AI pode organizar ideias, analisar materiais de estudo e responder a perguntas. As respostas aceitam Markdown.",
    "en": "NotePatch AI can organize ideas, analyze study materials, and answer questions. Responses support Markdown.",
}


def chat_greeting(locale: str) -> dict[str, str]:
    language = locale.split("-", 1)[0].lower()
    selected_language = language if language in _GREETINGS else "en"
    return {
        "assistant_name": "NotePatch AI",
        "message": _GREETINGS[selected_language],
        "message_key": "ai.chat.initial_greeting",
        "format": "markdown",
        "locale": locale,
    }

