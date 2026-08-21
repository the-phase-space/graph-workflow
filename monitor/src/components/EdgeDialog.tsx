// Edge, graph-settings, and Generator dialogs. An edge is first-class: one
// selectable object with N inputs, M outputs, one gate per connected node,
// and one universal gate over all inputs.

import { normalizeEdgeGates } from "../graphops";
import {
  GEN,
  type EdgeSpec,
  type GateSpec,
  type GraphModel,
} from "../types";
import { AreaField, Field, KV, TextField } from "./fields";

function GateEditor(props: {
  label: string;
  gate: GateSpec;
  editable: boolean;
  onChange: (g: GateSpec) => void;
}) {
  const g = props.gate;
  const isDefault = (g.expression ?? "True").trim() === "True" && g.p_invocation == null;
  return (
    <div className={"gate-editor" + (isDefault ? "" : " nondefault")}>
      <div className="gate-label">
        {props.label}
        {!isDefault && <span className="gate-flag">non-default</span>}
      </div>
      <div className="gate-row">
        <input
          className="mono gate-expr"
          value={g.expression}
          readOnly={!props.editable}
          spellCheck={false}
          onChange={(e) => props.onChange({ ...g, expression: e.target.value })}
        />
        <input
          className="mono gate-p"
          value={g.p_invocation == null ? "" : String(g.p_invocation)}
          placeholder="p"
          title="p_invocation ∈ [0, 1] — bound to the gate's seeded draw"
          readOnly={!props.editable}
          onChange={(e) =>
            props.onChange({
              ...g,
              p_invocation: e.target.value.trim() === "" ? null : Number(e.target.value),
            })
          }
        />
      </div>
    </div>
  );
}

export function EdgeDialog(props: {
  edge: EdgeSpec;
  model: GraphModel;
  editable: boolean;
  onChange: (e: EdgeSpec) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const { edge, model, editable } = props;
  const nodeIds = [GEN, ...model.nodes.map((n) => n.id)];
  const set = (patch: Partial<EdgeSpec>) =>
    props.onChange(normalizeEdgeGates({ ...edge, ...patch }));

  const toggle = (side: "inputs" | "outputs", id: string) => {
    const list = edge[side];
    set({ [side]: list.includes(id) ? list.filter((x) => x !== id) : [...list, id] });
  };

  return (
    <div className="dialog">
      <div className="dialog-head">
        <span className="dialog-title mono">{edge.id}</span>
        <span className="dialog-sub">
          edge · {edge.inputs.length} → {edge.outputs.length}
        </span>
        <button className="icon-btn" onClick={props.onClose} title="Close">
          ×
        </button>
      </div>
      <div className="dialog-body">
        <div className="subsection">
          <TextField
            label="Edge ID"
            value={edge.id}
            mono
            readOnly={!editable}
            onChange={editable ? (v) => props.onChange({ ...edge, id: v }) : undefined}
          />
        </div>
        <div className="subsection endpoint-cols">
          <div>
            <div className="field-label">Input nodes</div>
            {nodeIds.map((id) => (
              <label className="checkbox" key={id}>
                <input
                  type="checkbox"
                  disabled={!editable}
                  checked={edge.inputs.includes(id)}
                  onChange={() => toggle("inputs", id)}
                />
                <span className="mono">{id === GEN ? "Generator" : id}</span>
              </label>
            ))}
          </div>
          <div>
            <div className="field-label">Output nodes</div>
            {nodeIds
              .filter((id) => id !== GEN)
              .map((id) => (
                <label className="checkbox" key={id}>
                  <input
                    type="checkbox"
                    disabled={!editable}
                    checked={edge.outputs.includes(id)}
                    onChange={() => toggle("outputs", id)}
                  />
                  <span className="mono">{id}</span>
                </label>
              ))}
          </div>
        </div>
        <div className="subsection">
          <div className="section-title">Gates</div>
          <GateEditor
            label="Universal gate (all inputs; single shared draw)"
            gate={edge.universal_gate}
            editable={editable}
            onChange={(g) => set({ universal_gate: g })}
          />
          {edge.inputs.map((id) => (
            <GateEditor
              key={"in" + id}
              label={`Input gate — ${id === GEN ? "Generator" : id}`}
              gate={edge.input_gates[id] ?? { expression: "True" }}
              editable={editable}
              onChange={(g) => set({ input_gates: { ...edge.input_gates, [id]: g } })}
            />
          ))}
          {edge.outputs.map((id) => (
            <GateEditor
              key={"out" + id}
              label={`Output gate — ${id}`}
              gate={edge.output_gates[id] ?? { expression: "True" }}
              editable={editable}
              onChange={(g) => set({ output_gates: { ...edge.output_gates, [id]: g } })}
            />
          ))}
        </div>
        {editable && (
          <div className="subsection">
            <button className="danger-btn" onClick={props.onDelete}>
              Delete edge
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function GeneratorDialog(props: { fired: boolean; onClose: () => void }) {
  return (
    <div className="dialog">
      <div className="dialog-head">
        <span className="dialog-title mono">Generator</span>
        <button className="icon-btn" onClick={props.onClose} title="Close">
          ×
        </button>
      </div>
      <div className="dialog-body">
        <div className="subsection">
          <KV k="output state" v={props.fired ? "TRUE" : "FALSE"} />
          <p className="hint-text">
            Exactly one Generator exists per graph. It has a single property — an
            output state that starts FALSE — and no logic, so nothing can be
            upstream of it. At run start its output state flips to TRUE; that flip
            is the run's first event and seeds the initial ready set.
          </p>
        </div>
      </div>
    </div>
  );
}

export function GraphDialog(props: {
  model: GraphModel;
  helpers: string;
  editable: boolean;
  onChange: (m: GraphModel) => void;
  onHelpers: (h: string) => void;
  onClose: () => void;
}) {
  const { model, editable } = props;
  const set = (patch: Partial<GraphModel>) => props.onChange({ ...model, ...patch });
  const sems = model.semaphores;
  const setSem = (k: "script" | "llm" | "human", v: string) =>
    set({ semaphores: { ...sems, [k]: Math.max(1, parseInt(v, 10) || 1) } });

  return (
    <div className="dialog">
      <div className="dialog-head">
        <span className="dialog-title">Graph settings</span>
        <button className="icon-btn" onClick={props.onClose} title="Close">
          ×
        </button>
      </div>
      <div className="dialog-body">
        <div className="subsection">
          <TextField
            label="Graph name"
            value={model.name}
            readOnly={!editable}
            onChange={editable ? (v) => set({ name: v }) : undefined}
          />
        </div>
        <div className="subsection">
          <div className="section-title">Semaphores</div>
          <div className="sem-grid">
            {(["script", "llm", "human"] as const).map((k) => (
              <Field label={k} key={k}>
                <input
                  className="mono"
                  value={String(sems[k])}
                  readOnly={!editable}
                  onChange={(e) => setSem(k, e.target.value)}
                />
              </Field>
            ))}
          </div>
          <div className="field-label">Per-provider (subdivide the LLM semaphore)</div>
          {["claude", "codex", "grok"].map((p) => (
            <Field label={p} key={p}>
              <input
                className="mono"
                value={sems.providers[p] == null ? "" : String(sems.providers[p])}
                placeholder="unbounded"
                readOnly={!editable}
                onChange={(e) => {
                  const providers = { ...sems.providers };
                  const v = e.target.value.trim();
                  if (!v) delete providers[p];
                  else providers[p] = Math.max(1, parseInt(v, 10) || 1);
                  set({ semaphores: { ...sems, providers } });
                }}
              />
            </Field>
          ))}
        </div>
        <div className="subsection">
          <div className="section-title">Pause / Resume messages</div>
          <AreaField
            label="Pause message (all LLM nodes)"
            value={model.pause_message ?? ""}
            rows={2}
            placeholder="runtime default"
            readOnly={!editable}
            onChange={editable ? (v) => set({ pause_message: v || null }) : undefined}
          />
          <AreaField
            label="Resume message (all LLM nodes)"
            value={model.resume_message ?? ""}
            rows={2}
            placeholder="runtime default"
            readOnly={!editable}
            onChange={editable ? (v) => set({ resume_message: v || null }) : undefined}
          />
        </div>
        <div className="subsection">
          <div className="section-title">Helpers (preserved verbatim)</div>
          <AreaField
            label="Linked helper scripts / functions"
            value={props.helpers}
            rows={12}
            readOnly={!editable}
            hint="Python placed in the definition's gw:helpers section; reference by name from nodes and expressions"
            onChange={editable ? props.onHelpers : undefined}
          />
        </div>
      </div>
    </div>
  );
}
