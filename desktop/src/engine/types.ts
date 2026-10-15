// Types mirroring flowengine's service payloads.

export type FieldDef = { name: string; type: string; size?: number; scale?: number };

export type AnchorDef = { name: string; label: string; required: boolean; multiple: boolean; description: string };

export type ToolManifest = {
  tool_id: string;
  version: string;
  display_name: string;
  catalog_ids: string[];
  inputs: AnchorDef[];
  outputs: AnchorDef[];
  config_schema: JsonSchema;
  side_effects: string;
  deterministic: boolean;
  dependencies: string[];
  description: string;
};

export type CatalogEntry = {
  id: string;
  name: string;
  category: string;
  implementation_status: 'implemented' | 'in-progress' | 'not-started' | 'blocked';
  verification_status: 'not-tested' | 'partial' | 'verified' | 'failed' | 'blocked';
  kernel: string | null;
  documentation_url: string | null;
  external_prerequisites_blocking: boolean;
  unavailable_reason: string | null;
};

export type JsonSchema = {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  required?: string[];
  enum?: unknown[];
  default?: unknown;
  description?: string;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  oneOf?: JsonSchema[];
  additionalProperties?: boolean | JsonSchema;
  const?: unknown;
};

export type Problem = { code: string; message: string; node_id: string | null };

export type ValidateResult = { valid: boolean; problems: Problem[]; schemas: Record<string, Record<string, FieldDef[]>> };

export type Preview = { fields: FieldDef[]; total_rows: number; rows: (string | null)[][] };

export type EngineEvent = {
  seq: number;
  run_id: string;
  ts: string;
  type: string;
  node_id: string | null;
  message?: string;
  code?: string;
  duration_ms?: number;
  output_rows?: Record<string, number>;
  input_rows?: Record<string, number>;
  [k: string]: unknown;
};

export type RunPayload = {
  run_id: string;
  status: 'succeeded' | 'failed' | 'cancelled' | 'invalid';
  error: { code: string; message: string; node_id: string | null } | null;
  manifest: any;
  events: EngineEvent[];
  previews: Record<string, Record<string, Preview>>;
  browse: Record<string, Preview>;
  problems?: Problem[];
};

export type Profile = { name: string; product_build: string; engine: string; status: string; notes: string[] };

export type HistoryEntry = {
  run_id: string;
  started_utc: string;
  finished_utc: string;
  status: string;
  workflow_name: string | null;
  workflow_digest: string;
  profile: string;
  engine_version: string;
  duration_ms: number | null;
};

// flowengine workflow document (schemas/workflow.schema.json)
export type WorkflowDoc = {
  format: 'flowengine.workflow';
  formatVersion: 1;
  name?: string;
  description?: string;
  metadata?: Record<string, any>;
  constants?: Record<string, string | number | boolean | null>;
  nodes: {
    id: string;
    tool: string;
    toolVersion?: string;
    config: Record<string, unknown>;
    label?: string;
    disabled?: boolean;
    position?: { x: number; y: number };
    annotation?: string;
  }[];
  edges: { from: { node: string; anchor: string }; to: { node: string; anchor: string } }[];
};
