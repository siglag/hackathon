export type Case = { id: string; label: string; created_at: number; updated_at: number };
export type Conversation = { id: string; title: string; model: string; created_at: number };

export type Doc = {
  id: string;
  filename: string;
  kind: "image" | "pdf" | "xlsx" | "csv" | "json";
  state: string;
  source: string;
  page_count: number | null;
  created_at: number;
  extraction_state: string | null;
  unreviewed_critical: number;
  datasets: string[];
  notes: string[];
  quality: any[];
  embedded_images: number;
  job_id?: string;
};

export type Column = { name: string; type: string; derived?: boolean };

export type Dataset = {
  id: string;
  label: string;
  version: number;
  lineage_id: string;
  parent_id: string | null;
  review_state: "reviewed" | "draft" | "imported";
  columns: Column[];
  row_count: number;
  transformations: any[];
  source_documents: string[];
  created_at: number;
  meta: { notes?: string[]; unreviewed_critical?: number };
};

export type Artifact = {
  id: string;
  title: string;
  format: string;
  version: number;
  lineage_id: string;
  dataset_id: string | null;
  review_state: string;
  created_at: number;
  preview_kind: string;
  size: number;
};

export type Cell = { v: any; r?: string; src?: any; rs?: string; unit?: string; unavailable?: string; formula?: string; score?: number };
export type Row = { id: string; cells: Record<string, Cell> };

export type EvidenceQuery = {
  id: string;
  status: string;
  request: { concepts: string[]; population: string | null; study_types: string[]; year_from: number | null; year_to: number | null };
  request_hash: string;
  description: string;
  search_string: string;
  held_terms: string[];
  problems: string[];
  blocked_types: string[];
  outcome: any;
};

export type Citation = {
  ref: string;
  pmid: string;
  doi?: string;
  title: string;
  authors: string[];
  journal?: string;
  year?: string;
  design: string;
  flags: string[];
  pubmed_url?: string;
  doi_url?: string;
  access: string;
  passages: { id: string; text: string; from: string }[];
};

export type Claim = { text: string; refs: string[]; weak_support?: boolean; records?: string[] };

export type Answer = {
  findings: Claim[];
  case_interpretation: Claim[];
  unanswered: string[];
  conflicts: string[];
  limitations: string[];
  citations: Citation[];
  records?: { ref: string; dataset_id: string; row_id: string; text: string }[];
  retrieved_at?: number;
  query?: { id: string; description: string; effective_query: string };
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  meta: any;
  job_id: string | null;
  created_at: number;
};

export type Selection =
  | { type: "dataset"; id: string }
  | { type: "artifact"; id: string }
  | { type: "document"; id: string }
  | { type: "review"; id: string };
