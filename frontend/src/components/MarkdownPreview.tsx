import { marked } from "marked";

interface Props {
  markdown: string;
  className?: string;
}

export function MarkdownPreview({ markdown, className }: Props) {
  const html = marked(markdown, { async: false });

  return (
    <div
      className={[
        "overflow-y-auto",
        "text-sm text-gray-800 leading-relaxed",
        "[&_h1]:text-2xl [&_h1]:font-bold [&_h1]:mt-4 [&_h1]:mb-2",
        "[&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1",
        "[&_h3]:text-lg [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-1",
        "[&_p]:mb-3",
        "[&_ul]:list-disc [&_ul]:pl-5 [&_ul]:mb-3",
        "[&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:mb-3",
        "[&_li]:mb-1",
        "[&_strong]:font-semibold",
        "[&_em]:italic",
        "[&_hr]:my-4 [&_hr]:border-gray-300",
        className ?? "",
      ].join(" ")}
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
