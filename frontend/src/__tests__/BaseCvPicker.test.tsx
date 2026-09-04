import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BaseCvPicker } from "../components/BaseCvPicker";
import { useStore } from "../store";
import type { CvDeckDTO, JobDTO } from "../types";

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    state: "queued",
    current_stage: null,
    backend_name: null,
    error: null,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    base_cv_id: null,
    ...overrides,
  };
}

const DECKS: CvDeckDTO[] = [
  { id: "d1", name: "Backend Focus", auto_title: "Jane Doe", has_cv: true, is_default: true },
  { id: "d2", name: null, auto_title: "Frontend Deck", has_cv: true, is_default: false },
  { id: "d3", name: null, auto_title: null, has_cv: false, is_default: false },
];

function seed(overrides: Partial<ReturnType<typeof useStore.getState>> = {}) {
  const job = makeJob();
  useStore.setState({
    jobs: { [job.id]: job },
    cvPickerJobId: job.id,
    cvPickerPos: { x: 20, y: 30 },
    cvDecks: DECKS,
    cvDecksDefaultId: "d1",
    closeCvPicker: vi.fn(),
    assignBaseCv: vi.fn(),
    setEditorOpen: vi.fn(),
    ...overrides,
  });
}

beforeEach(() => {
  useStore.setState({ language: "en" });
  seed();
});

describe("BaseCvPicker", () => {
  it("renders nothing when no picker is open", () => {
    useStore.setState({ cvPickerJobId: null });
    const { container } = render(<BaseCvPicker />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists every has_cv deck, omitting the empty (has_cv:false) slot", () => {
    render(<BaseCvPicker />);
    expect(screen.getByTestId("base-cv-picker-deck-d1")).toBeInTheDocument();
    expect(screen.getByTestId("base-cv-picker-deck-d2")).toBeInTheDocument();
    expect(screen.queryByTestId("base-cv-picker-deck-d3")).not.toBeInTheDocument();
  });

  it("tags the default deck DEFAULT", () => {
    render(<BaseCvPicker />);
    const d1 = screen.getByTestId("base-cv-picker-deck-d1");
    expect(d1).toHaveTextContent("DEFAULT");
    const d2 = screen.getByTestId("base-cv-picker-deck-d2");
    expect(d2).not.toHaveTextContent("DEFAULT");
  });

  it("marks the currently assigned deck with a check, and no other row", () => {
    seed({ jobs: { job1: makeJob({ base_cv_id: "d2" }) } });
    render(<BaseCvPicker />);
    expect(screen.getByTestId("base-cv-picker-check-d2")).toBeInTheDocument();
    expect(screen.queryByTestId("base-cv-picker-check-d1")).not.toBeInTheDocument();
    expect(screen.getByTestId("base-cv-picker-unassign")).toBeInTheDocument();
  });

  it("does not render an UNASSIGN footer when nothing is assigned", () => {
    render(<BaseCvPicker />);
    expect(screen.queryByTestId("base-cv-picker-unassign")).not.toBeInTheDocument();
  });

  it("clicking a deck row calls assignBaseCv with that deck's id", () => {
    render(<BaseCvPicker />);
    fireEvent.click(screen.getByTestId("base-cv-picker-deck-d2"));
    expect(useStore.getState().assignBaseCv).toHaveBeenCalledWith("job1", "d2");
  });

  it("clicking UNASSIGN calls assignBaseCv with null", () => {
    seed({ jobs: { job1: makeJob({ base_cv_id: "d2" }) } });
    render(<BaseCvPicker />);
    fireEvent.click(screen.getByTestId("base-cv-picker-unassign"));
    expect(useStore.getState().assignBaseCv).toHaveBeenCalledWith("job1", null);
  });

  it("closing via the × button calls closeCvPicker", () => {
    render(<BaseCvPicker />);
    fireEvent.click(screen.getByTitle("Dismiss"));
    expect(useStore.getState().closeCvPicker).toHaveBeenCalled();
  });

  it("outside click closes the picker", () => {
    render(<BaseCvPicker />);
    fireEvent.mouseDown(document.body);
    expect(useStore.getState().closeCvPicker).toHaveBeenCalled();
  });

  it("shows the empty state and opens the Structure Editor when no deck has a CV yet", () => {
    seed({ cvDecks: [DECKS[2]] });
    render(<BaseCvPicker />);
    expect(screen.getByTestId("base-cv-picker-empty")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("base-cv-picker-open-editor"));
    expect(useStore.getState().setEditorOpen).toHaveBeenCalledWith(true);
    expect(useStore.getState().closeCvPicker).toHaveBeenCalled();
  });
});
