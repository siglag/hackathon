import DOMPurify from "dompurify";
import { marked } from "marked";

export function sanitize(html: string): string {
  // External links open only on click, without referrer; no remote images.
  const clean = DOMPurify.sanitize(html, { FORBID_TAGS: ["img", "iframe", "style", "form", "video", "audio", "object", "embed"], FORBID_ATTR: ["style", "srcset"] });
  const div = document.createElement("div");
  div.innerHTML = clean;
  div.querySelectorAll("a").forEach((el) => {
    el.setAttribute("target", "_blank");
    el.setAttribute("rel", "noopener noreferrer");
  });
  return div.innerHTML;
}

/** GitHub-flavoured markdown to sanitized HTML. `breaks` keeps single newlines, as chat replies expect. */
export function renderMarkdown(md: string, opts: { breaks?: boolean } = {}): string {
  return sanitize(marked.parse(md, { async: false, gfm: true, breaks: opts.breaks ?? false }) as string);
}
