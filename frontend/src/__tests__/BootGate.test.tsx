import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BootGate } from "../components/BootGate";
import { useStore } from "../store";

vi.mock("../api", () => ({
  api: {
    putPreferences: vi.fn().mockResolvedValue({ language: "es" }),
  },
}));

import { api } from "../api";

const LANGUAGES: [string, string, string][] = [
  ["en", "English", "English"],
  ["es", "Spanish", "Español"],
];

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({
    language: "en",
    languages: LANGUAGES,
    selectLanguageMode: true,
    bootStage: "lang",
    bootLang: "en",
  });
});

describe("BootGate", () => {
  it("renders nothing once bootStage is 'app'", () => {
    useStore.setState({ bootStage: "app" });
    const { container } = render(<BootGate />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the language picker with the full catalog when bootStage is 'lang'", () => {
    render(<BootGate />);
    expect(screen.getByText(/select output language before first run/i)).toBeInTheDocument();
    expect(screen.getByText("English")).toBeInTheDocument();
    expect(screen.getByText("Spanish")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /confirm & boot/i })).toBeInTheDocument();
  });

  it("selecting a pill updates bootLang locally without calling the API yet", async () => {
    render(<BootGate />);
    await userEvent.click(screen.getByText("Spanish"));
    expect(useStore.getState().bootLang).toBe("es");
    expect(api.putPreferences).not.toHaveBeenCalled();
  });

  it("CONFIRM & BOOT persists the selected language and advances to the boot log", async () => {
    render(<BootGate />);
    await userEvent.click(screen.getByText("Spanish"));
    await userEvent.click(screen.getByRole("button", { name: /confirm & boot/i }));

    await waitFor(() => expect(api.putPreferences).toHaveBeenCalledWith("es"));
    await waitFor(() => expect(useStore.getState().bootStage).toBe("boot"));
  });

  it("renders the boot log header and the first boot line immediately when bootStage is 'boot'", () => {
    useStore.setState({ bootStage: "boot", bootLang: "es" });
    render(<BootGate />);
    expect(screen.getByText(/BOOT SEQUENCE/)).toBeInTheDocument();
    expect(screen.getByText(/daemon\.init\(\)/)).toBeInTheDocument();
  });

  it("does not auto-advance the picker without an explicit confirm click", () => {
    render(<BootGate />);
    expect(useStore.getState().bootStage).toBe("lang");
  });

  it("stays on the picker and shows an error when the language PUT fails", async () => {
    vi.mocked(api.putPreferences).mockRejectedValueOnce(new Error("500"));
    render(<BootGate />);
    await userEvent.click(screen.getByText("Spanish"));
    await userEvent.click(screen.getByRole("button", { name: /confirm & boot/i }));

    await waitFor(() => expect(api.putPreferences).toHaveBeenCalledWith("es"));
    expect(useStore.getState().bootStage).toBe("lang");
    expect(await screen.findByText(/could not save language preference/i)).toBeInTheDocument();
  });
});
