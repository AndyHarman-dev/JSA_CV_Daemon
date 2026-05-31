"""Renderer registry: renderer_for(name) -> Renderer."""

from jsa.render.base import Renderer
from jsa.render.docx_render import DocxRenderer
from jsa.render.weasy import WeasyPrintRenderer

_REGISTRY: dict[str, type[Renderer]] = {
    "weasyprint": WeasyPrintRenderer,
    "docx": DocxRenderer,
}


def renderer_for(name: str) -> Renderer:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown renderer: {name!r}. Available: {list(_REGISTRY)}")
    return _REGISTRY[name]()
