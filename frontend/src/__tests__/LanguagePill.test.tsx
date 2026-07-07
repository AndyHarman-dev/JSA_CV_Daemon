import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LanguagePill } from "../components/cv-editor/LanguagePill";
import { useStore } from "../store";

vi.mock("../api", () => ({
  api: {
    putPreferences: vi.fn().mockResolvedValue({ language: "es" }),
    getPreferences: vi.fn().mockResolvedValue({ language: "en" }),
    config: vi.fn().mockResolvedValue({ languages: [] }),
  },
}));

import { api } from "../api";

const LANGUAGES: [string, string, string][] = [
  ["en", "English", "English"],
  ["es", "Spanish", "Español"],
  ["ja", "Japanese", "日本語"],
];

beforeEach(() => {
  useStore.setState({ language: "en", languages: LANGUAGES });
  vi.clearAllMocks();
});

describe("LanguagePill", () => {
  it("renders the current language code, closed by default", () => {
    render(<LanguagePill />);
    expect(screen.getByText("EN")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/search languages/i)).not.toBeInTheDocument();
  });

  it("opens the panel on click, listing every language", async () => {
    render(<LanguagePill />);
    await userEvent.click(screen.getByText("EN"));
    expect(screen.getByPlaceholderText(/search languages/i)).toBeInTheDocument();
    expect(screen.getByText("Spanish")).toBeInTheDocument();
    expect(screen.getByText("Japanese")).toBeInTheDocument();
  });

  it("filters rows as the search query changes", async () => {
    render(<LanguagePill />);
    await userEvent.click(screen.getByText("EN"));
    await userEvent.type(screen.getByPlaceholderText(/search languages/i), "jap");
    expect(screen.getByText("Japanese")).toBeInTheDocument();
    expect(screen.queryByText("Spanish")).not.toBeInTheDocument();
  });

  it("shows the no-matches state for an unmatched query", async () => {
    render(<LanguagePill />);
    await userEvent.click(screen.getByText("EN"));
    await userEvent.type(screen.getByPlaceholderText(/search languages/i), "zzz");
    expect(screen.getByText(/no matches/i)).toBeInTheDocument();
  });

  it("selecting a row calls setLanguage, updates the store, and closes the panel", async () => {
    render(<LanguagePill />);
    await userEvent.click(screen.getByText("EN"));
    await userEvent.click(screen.getByText("Spanish"));

    await waitFor(() => expect(useStore.getState().language).toBe("es"));
    expect(api.putPreferences).toHaveBeenCalledWith("es");
    expect(screen.queryByPlaceholderText(/search languages/i)).not.toBeInTheDocument();
  });

  it("closes the panel on an outside click", async () => {
    render(
      <div>
        <div data-testid="outside">outside</div>
        <LanguagePill />
      </div>
    );
    await userEvent.click(screen.getByText("EN"));
    expect(screen.getByPlaceholderText(/search languages/i)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("outside"));
    expect(screen.queryByPlaceholderText(/search languages/i)).not.toBeInTheDocument();
  });
});
