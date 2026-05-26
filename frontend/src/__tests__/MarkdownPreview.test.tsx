import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownPreview } from "../components/MarkdownPreview";

describe("MarkdownPreview", () => {
  it("renders a markdown h1 heading as an <h1> element", () => {
    const { container } = render(<MarkdownPreview markdown="# Hello World" />);
    const h1 = container.querySelector("h1");
    expect(h1).not.toBeNull();
    expect(h1?.textContent).toBe("Hello World");
  });

  it("renders a markdown h2 heading as an <h2> element", () => {
    const { container } = render(<MarkdownPreview markdown="## Subtitle" />);
    const h2 = container.querySelector("h2");
    expect(h2).not.toBeNull();
    expect(h2?.textContent).toBe("Subtitle");
  });

  it("renders **bold** text as <strong>", () => {
    const { container } = render(<MarkdownPreview markdown="This is **bold** text." />);
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("bold");
  });

  it("renders plain paragraph text", () => {
    render(<MarkdownPreview markdown="Just a plain paragraph." />);
    expect(screen.getByText("Just a plain paragraph.")).toBeInTheDocument();
  });

  it("accepts a className prop and applies it to the outer div", () => {
    const { container } = render(
      <MarkdownPreview markdown="hello" className="my-custom-class" />
    );
    const outer = container.firstElementChild as HTMLElement;
    expect(outer.classList.contains("my-custom-class")).toBe(true);
  });

  it("renders without error when given an empty string", () => {
    const { container } = render(<MarkdownPreview markdown="" />);
    // The outer div should still be present
    const outer = container.firstElementChild;
    expect(outer).not.toBeNull();
  });

  it("renders multiple headings correctly", () => {
    const { container } = render(
      <MarkdownPreview markdown={"# Title\n\n## Section\n\n### Subsection"} />
    );
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelector("h2")?.textContent).toBe("Section");
    expect(container.querySelector("h3")?.textContent).toBe("Subsection");
  });

  it("renders _italic_ text as <em>", () => {
    const { container } = render(<MarkdownPreview markdown="This is _italic_ text." />);
    const em = container.querySelector("em");
    expect(em).not.toBeNull();
    expect(em?.textContent).toBe("italic");
  });
});
