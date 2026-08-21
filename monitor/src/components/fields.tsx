// Small shared form primitives for the element dialogs. In execution mode
// every field renders read-only: the topology is frozen and edge and gate
// properties remain static for the duration of the run.

import type { ReactNode } from "react";

export function Field(props: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span className="field-label" title={props.hint}>
        {props.label}
      </span>
      {props.children}
    </label>
  );
}

export function TextField(props: {
  label: string;
  value: string;
  onChange?: (v: string) => void;
  readOnly?: boolean;
  mono?: boolean;
  placeholder?: string;
  list?: string;
  hint?: string;
}) {
  return (
    <Field label={props.label} hint={props.hint}>
      <input
        className={props.mono ? "mono" : undefined}
        value={props.value}
        placeholder={props.placeholder}
        readOnly={props.readOnly || !props.onChange}
        onChange={(e) => props.onChange?.(e.target.value)}
        list={props.list}
        spellCheck={false}
      />
    </Field>
  );
}

export function AreaField(props: {
  label: string;
  value: string;
  onChange?: (v: string) => void;
  readOnly?: boolean;
  rows?: number;
  placeholder?: string;
  hint?: string;
}) {
  return (
    <Field label={props.label} hint={props.hint}>
      <textarea
        className="mono"
        rows={props.rows ?? 5}
        value={props.value}
        placeholder={props.placeholder}
        readOnly={props.readOnly || !props.onChange}
        onChange={(e) => props.onChange?.(e.target.value)}
        spellCheck={false}
      />
    </Field>
  );
}

export function SelectField(props: {
  label: string;
  value: string;
  options: string[];
  onChange?: (v: string) => void;
  readOnly?: boolean;
  allowEmpty?: boolean;
}) {
  return (
    <Field label={props.label}>
      <select
        value={props.value}
        disabled={props.readOnly || !props.onChange}
        onChange={(e) => props.onChange?.(e.target.value)}
      >
        {props.allowEmpty && <option value="">—</option>}
        {props.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </Field>
  );
}

/** name -> type editor for declared inputs/outputs, with per-name status
 * marking (received/written entries get the green outline). */
export function IoEditor(props: {
  label: string;
  entries: Record<string, string>;
  marked?: Set<string>;
  onChange?: (entries: Record<string, string>) => void;
}) {
  const rows = Object.entries(props.entries);
  const editable = !!props.onChange;
  const update = (idx: number, name: string, type: string) => {
    const out: Record<string, string> = {};
    rows.forEach(([n, t], i) => {
      if (i === idx) {
        if (name) out[name] = type;
      } else out[n] = t;
    });
    props.onChange?.(out);
  };
  return (
    <div className="io-editor">
      <div className="field-label">{props.label}</div>
      {rows.length === 0 && <div className="io-empty">none declared</div>}
      {rows.map(([name, type], i) => (
        <div className={"io-row" + (props.marked?.has(name) ? " marked" : "")} key={i}>
          <input
            className="mono io-name"
            value={name}
            readOnly={!editable}
            spellCheck={false}
            onChange={(e) => update(i, e.target.value, type)}
          />
          <span className="io-colon">:</span>
          <input
            className="mono io-type"
            value={type}
            readOnly={!editable}
            spellCheck={false}
            onChange={(e) => update(i, name, e.target.value)}
          />
          {editable && (
            <button
              className="icon-btn"
              title="Remove"
              onClick={() => update(i, "", type)}
            >
              ×
            </button>
          )}
        </div>
      ))}
      {editable && (
        <button
          className="io-add"
          onClick={() => {
            let n = 1;
            while (`out${n}` in props.entries) n++;
            props.onChange?.({ ...props.entries, [`out${n}`]: "str" });
          }}
        >
          + add
        </button>
      )}
    </div>
  );
}

export function KV(props: { k: string; v: ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{props.k}</span>
      <span className="v">{props.v}</span>
    </div>
  );
}
