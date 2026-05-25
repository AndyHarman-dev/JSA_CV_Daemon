"""Renderer ABC: async render(markdown, output_path) -> None."""

from abc import ABC, abstractmethod
from pathlib import Path


class Renderer(ABC):
    name: str

    @abstractmethod
    async def render(self, markdown: str, output_path: Path) -> None: ...
