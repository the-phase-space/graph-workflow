// The node dialog: all properties of the selected node, editable in build
// mode; in execution mode it adds runtime state, input/output status marks,
// and a scrollable, real-time activity-log sidebar with a `Show tool uses`
// checkbox.

import { useEffect, useRef, useState } from "react";
import { nodeLiveTokens, type RunState } from "../runstate";
import {
  EFFORTS,
  PROVIDER_MODELS,
  fmtTokens,
  nodeFill,
  type ActivityItem,
  type NodeSpec,
} from "../types";
import { AreaField, IoEditor, KV, SelectField, TextField } from "./fields";

export default function NodeDialog(props: {
  node: NodeSpec;
  run: RunState | null;
  editable: boolean;
  onChange: (n: NodeSpec) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const { node, run, editable } = props;
  const [showTools, setShowTools] = useState(false);
  const rt = run?.derived.nodes.get(node.id);
  const statusNode = run?.status?.nodes?.[node.id];
  const activity = run?.activity.get(node.id) ?? [];

  const set = (patch: Partial<NodeSpec>) => props.onChange({ ...node, ...patch });
  const ro = !editable;

  const received = rt?.receivedInputs ?? new Set<string>();
  const written = rt?.writtenOutputs ?? new Set<string>();
  const live = run ? nodeLiveTokens(run.liveTokens, node.id, statusNode) : 0;

  return (
    <div className="dialog node-dialog">
      <div className="dialog-head">
        <span className="swatch" style={{ background: nodeFill(node) }} />
        <span className="dialog-title mono">{node.id}</span>
        <span className="dialog-sub">
          {node.type === "llm" ? `${node.provider} · ${node.model ?? ""}` : node.type}
        </span>
        <button className="icon-btn" onClick={props.onClose} title="Close">
          ×
        </button>
      </div>

      <div className="dialog-body">
        <div className="dialog-cols">
          <div className="dialog-main">
            {run && (
              <div className="subsection">
                <div className="section-title">Runtime</div>
                <KV k="state" v={statusNode?.state ?? "idle"} />
                <KV
                  k="output state"
                  v={String(
                    statusNode ? statusNode.output_state : rt?.outputState ?? "UNSET",
                  )}
                />
                <KV k="invocations" v={String(statusNode?.invocations ?? 0)} />
                <KV k="commits" v={String(statusNode?.commits ?? 0)} />
                {node.type === "llm" && (
                  <>
                    <KV
                      k="tokens"
                      v={fmtTokens((rt?.tokensCommitted ?? 0) + live)}
                    />
                    <KV k="context size" v={fmtTokens(rt?.contextSize ?? 0)} />
                    {rt?.sessionId && <KV k="session" v={<code>{rt.sessionId}</code>} />}
                  </>
                )}
                {rt?.durationS != null && <KV k="last duration" v={`${rt.durationS}s`} />}
                {rt?.lastError && <div className="issue err">{rt.lastError}</div>}
              </div>
            )}

            <div className="subsection">
              <div className="section-title">Identity</div>
              <TextField
                label="Node ID"
                value={node.id}
                mono
                readOnly={ro}
                onChange={editable ? (v) => set({ id: v }) : undefined}
              />
              <TextField
                label="Rank"
                value={String(node.rank)}
                mono
                readOnly={ro}
                onChange={
                  editable
                    ? (v) => set({ rank: parseInt(v, 10) || 0 })
                    : undefined
                }
                hint="Unique; scheduling proceeds by rank"
              />
            </div>

            <div className="subsection">
              <div className="section-title">Expressions</div>
              <AreaField
                label="Gating expression"
                value={node.gate ?? ""}
                rows={2}
                placeholder="default: AND of all in-edge terms"
                readOnly={ro}
                onChange={editable ? (v) => set({ gate: v || null }) : undefined}
              />
              <AreaField
                label="Output expression"
                value={node.output_expression ?? ""}
                rows={2}
                placeholder="default: isDefined() over every declared output"
                readOnly={ro}
                onChange={
                  editable ? (v) => set({ output_expression: v || null }) : undefined
                }
              />
            </div>

            {node.type === "llm" && (
              <div className="subsection">
                <div className="section-title">Model invocation</div>
                <SelectField
                  label="Provider"
                  value={node.provider ?? "claude"}
                  options={["claude", "codex", "grok"]}
                  readOnly={ro}
                  onChange={editable ? (v) => set({ provider: v }) : undefined}
                />
                <TextField
                  label="Model"
                  value={node.model ?? ""}
                  mono
                  list={`models-${node.provider}`}
                  readOnly={ro}
                  onChange={editable ? (v) => set({ model: v }) : undefined}
                />
                <datalist id={`models-${node.provider}`}>
                  {(PROVIDER_MODELS[node.provider ?? ""] ?? []).map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                <SelectField
                  label="Effort"
                  value={node.effort ?? ""}
                  options={EFFORTS}
                  allowEmpty
                  readOnly={ro}
                  onChange={editable ? (v) => set({ effort: v || null }) : undefined}
                />
                <AreaField
                  label="Prompt"
                  value={node.prompt ?? (node.prompt_ref ? `<callable: ${node.prompt_ref}>` : "")}
                  rows={7}
                  readOnly={ro || node.prompt == null && !!node.prompt_ref}
                  hint="$name placeholders substitute captured inputs"
                  onChange={
                    editable && !(node.prompt == null && node.prompt_ref)
                      ? (v) => set({ prompt: v })
                      : undefined
                  }
                />
                <TextField
                  label="Settings file"
                  value={node.settings ?? ""}
                  mono
                  readOnly={ro}
                  hint="Hook/settings file passed to the CLI (tool permissions live here)"
                  onChange={editable ? (v) => set({ settings: v || null }) : undefined}
                />
                <TextField
                  label="Working directory"
                  value={node.cwd ?? ""}
                  mono
                  readOnly={ro}
                  onChange={editable ? (v) => set({ cwd: v || null }) : undefined}
                />
                <TextField
                  label="Extra CLI args"
                  value={(node.extra_args ?? []).join(" ")}
                  mono
                  readOnly={ro}
                  hint="Space-separated; appended to the provider invocation"
                  onChange={
                    editable
                      ? (v) => set({ extra_args: v.trim() ? v.trim().split(/\s+/) : [] })
                      : undefined
                  }
                />
                <TextField
                  label="Timeout (s)"
                  value={node.timeout_s == null ? "" : String(node.timeout_s)}
                  mono
                  readOnly={ro}
                  onChange={
                    editable
                      ? (v) => set({ timeout_s: v.trim() ? Number(v) : null })
                      : undefined
                  }
                />
                <TextField
                  label="Pause message"
                  value={node.pause_message ?? ""}
                  readOnly={ro}
                  placeholder="graph / runtime default"
                  onChange={editable ? (v) => set({ pause_message: v || null }) : undefined}
                />
                <TextField
                  label="Resume message"
                  value={node.resume_message ?? ""}
                  readOnly={ro}
                  placeholder="graph / runtime default"
                  onChange={editable ? (v) => set({ resume_message: v || null }) : undefined}
                />
              </div>
            )}

            {node.type === "script" && (
              <div className="subsection">
                <div className="section-title">Script</div>
                {node.code != null || !node.code_ref ? (
                  <AreaField
                    label="Code (fn)"
                    value={node.code ?? node.code_opaque ?? ""}
                    rows={12}
                    readOnly={ro || (node.code == null && !!node.code_opaque)}
                    hint="A def taking (inputs) and returning a dict over declared outputs, or a lambda"
                    onChange={editable ? (v) => set({ code: v }) : undefined}
                  />
                ) : (
                  <TextField
                    label="Helper function"
                    value={node.code_ref ?? ""}
                    mono
                    readOnly={ro}
                    hint="Name of a function in the helpers section"
                    onChange={editable ? (v) => set({ code_ref: v || null }) : undefined}
                  />
                )}
              </div>
            )}

            {node.type === "human" && (
              <div className="subsection">
                <div className="section-title">Human shell</div>
                <AreaField
                  label="Prompt"
                  value={node.prompt ?? (node.prompt_ref ? `<callable: ${node.prompt_ref}>` : "")}
                  rows={5}
                  readOnly={ro || (node.prompt == null && !!node.prompt_ref)}
                  onChange={
                    editable && !(node.prompt == null && node.prompt_ref)
                      ? (v) => set({ prompt: v })
                      : undefined
                  }
                />
                <TextField
                  label="Shell command"
                  value={node.shell_cmd ?? ""}
                  mono
                  readOnly={ro}
                  placeholder="default human-shell executable"
                  onChange={editable ? (v) => set({ shell_cmd: v || null }) : undefined}
                />
                <TextField
                  label="Timeout (s)"
                  value={node.timeout_s == null ? "" : String(node.timeout_s)}
                  mono
                  readOnly={ro}
                  onChange={
                    editable
                      ? (v) => set({ timeout_s: v.trim() ? Number(v) : null })
                      : undefined
                  }
                />
              </div>
            )}

            <div className="subsection">
              <div className="section-title">Data plane</div>
              <IoEditor
                label="Declared inputs"
                entries={node.inputs}
                marked={received}
                onChange={editable ? (v) => set({ inputs: v }) : undefined}
              />
              <IoEditor
                label="Declared outputs"
                entries={node.outputs}
                marked={written}
                onChange={editable ? (v) => set({ outputs: v }) : undefined}
              />
              {rt && Object.keys(rt.outputs).length > 0 && (
                <div className="outputs-view">
                  {Object.entries(rt.outputs).map(([k, v]) => (
                    <div className="output-preview" key={k}>
                      <span className="mono k">{k}</span>
                      <pre>{typeof v === "string" ? v : JSON.stringify(v, null, 1)}</pre>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {editable && (
              <div className="subsection">
                <button className="danger-btn" onClick={props.onDelete}>
                  Delete node
                </button>
              </div>
            )}
          </div>

          {run && (
            <div className="activity-pane">
              <div className="activity-head">
                <span className="section-title">Activity</span>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={showTools}
                    onChange={(e) => setShowTools(e.target.checked)}
                  />
                  Show tool uses
                </label>
              </div>
              <ActivityLog items={activity} showTools={showTools} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ActivityLog(props: { items: ActivityItem[]; showTools: boolean }) {
  const items = props.items.filter(
    (i) => props.showTools || (i.kind !== "tool" && i.kind !== "tool-result"),
  );
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [items.length]);
  return (
    <div
      className="activity-log"
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      {items.length === 0 && <div className="io-empty">no session activity yet</div>}
      {items.map((item, i) => (
        <div className={"act act-" + item.kind} key={i}>
          <div className="act-label">{item.label}</div>
          {item.text && <pre className="act-text">{item.text}</pre>}
        </div>
      ))}
    </div>
  );
}
