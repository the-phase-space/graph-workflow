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

/** Grid row placing short fields side by side instead of stacked. */
export function FieldRow(props: { cols?: 2 | 3 | "2-1"; children: ReactNode }) {
  return <div className={`field-row cols-${props.cols ?? 2}`}>{props.children}</div>;
}

/** The inspector panel's minimize control: a collapse-to-sidebar glyph, not a
 * close ×. Clicking empty canvas does the same thing. */
export function CollapseButton(props: { onClick: () => void }) {
  return (
    <button className="icon-btn collapse-btn" onClick={props.onClick} title="Minimize panel">
      <svg
        width="15"
        height="15"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      >
        <rect x="1.7" y="2.7" width="12.6" height="10.6" rx="2" />
        <line x1="10.3" y1="2.7" x2="10.3" y2="13.3" />
        <rect x="10.3" y="2.7" width="4" height="10.6" fill="currentColor" stroke="none" opacity="0.35" />
      </svg>
    </button>
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

export interface SelectOption {
  label: string;
  value: string;
}

export function SelectField(props: {
  label: string;
  value: string;
  options: (string | SelectOption)[];
  onChange?: (v: string) => void;
  readOnly?: boolean;
  allowEmpty?: boolean;
  hint?: string;
}) {
  const opts = props.options.map((o) =>
    typeof o === "string" ? { label: o, value: o } : o,
  );
  return (
    <Field label={props.label} hint={props.hint}>
      <select
        value={props.value}
        disabled={props.readOnly || !props.onChange}
        onChange={(e) => props.onChange?.(e.target.value)}
      >
        {props.allowEmpty && <option value="">—</option>}
        {opts.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
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
