// Small shared form primitives for the element dialogs. In execution mode
// every field renders read-only: the topology is frozen and edge and gate
// properties remain static for the duration of the run.

import { useEffect, useRef, useState, type ReactNode } from "react";

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

/** The […] browse affordance placed at the right end of a path field. */
function BrowseButton(props: { onClick: () => void; title: string }) {
  return (
    <button
      type="button"
      className="browse-btn"
      title={props.title}
      onClick={(e) => {
        e.preventDefault();
        props.onClick();
      }}
    >
      …
    </button>
  );
}

/** Grid row placing short fields side by side instead of stacked. */
export function FieldRow(props: { cols?: 2 | 3 | "2-1" | "2-1-2"; children: ReactNode }) {
  return <div className={`field-row cols-${props.cols ?? 2}`}>{props.children}</div>;
}

/** The inspector panel's minimize control: a collapse-to-sidebar glyph, not a
 * close ×. This is the only way the panel dismisses; canvas clicks never
 * collapse it. */
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
  /** Explorer picker; the returned path replaces the field value. */
  onBrowse?: () => Promise<string | null>;
}) {
  const input = (
    <input
      className={props.mono ? "mono" : undefined}
      value={props.value}
      placeholder={props.placeholder}
      readOnly={props.readOnly || !props.onChange}
      onChange={(e) => props.onChange?.(e.target.value)}
      list={props.list}
      spellCheck={false}
    />
  );
  const canBrowse = props.onBrowse && !props.readOnly && props.onChange;
  return (
    <Field label={props.label} hint={props.hint}>
      {canBrowse ? (
        <span className="input-browse">
          {input}
          <BrowseButton
            title={`Browse for ${props.label.toLowerCase()}`}
            onClick={() => {
              void props.onBrowse!().then((p) => {
                if (p != null) props.onChange?.(p);
              });
            }}
          />
        </span>
      ) : (
        input
      )}
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
  /** Explorer picker; the returned path is inserted at the cursor (appended
   * if the textarea has not been focused yet). */
  onBrowse?: () => Promise<string | null>;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const touched = useRef(false);
  const canBrowse = props.onBrowse && !props.readOnly && props.onChange;
  const browse = () => {
    void props.onBrowse!().then((p) => {
      if (p == null) return;
      const ta = taRef.current;
      const v = props.value;
      if (ta && touched.current) {
        const s = ta.selectionStart ?? v.length;
        const e = ta.selectionEnd ?? s;
        props.onChange?.(v.slice(0, s) + p + v.slice(e));
      } else {
        props.onChange?.(v + p);
      }
    });
  };
  return (
    <label className="field">
      <span className="field-label-row">
        <span className="field-label" title={props.hint}>
          {props.label}
        </span>
        {canBrowse && (
          <BrowseButton title={`Browse for a path to insert into the ${props.label.toLowerCase()}`} onClick={browse} />
        )}
      </span>
      <textarea
        ref={taRef}
        className="mono"
        rows={props.rows ?? 5}
        value={props.value}
        placeholder={props.placeholder}
        readOnly={props.readOnly || !props.onChange}
        onChange={(e) => props.onChange?.(e.target.value)}
        onFocus={() => (touched.current = true)}
        spellCheck={false}
      />
    </label>
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
 * marking (received/written entries get the green outline). Rows are local
 * state so a name can sit empty or duplicated mid-edit without its row
 * vanishing: such rows drop out of the committed record on every keystroke
 * but only leave the screen once focus leaves their row. */
export function IoEditor(props: {
  label: string;
  entries: Record<string, string>;
  marked?: Set<string>;
  onChange?: (entries: Record<string, string>) => void;
}) {
  const editable = !!props.onChange;
  const [rows, setRows] = useState<[string, string][]>(() => Object.entries(props.entries));
  const committed = useRef(JSON.stringify(props.entries));

  // An external change (undo, node switch, load) resets the rows; the echo
  // of our own commit must not, or the row being edited would vanish.
  useEffect(() => {
    if (JSON.stringify(props.entries) !== committed.current) {
      committed.current = JSON.stringify(props.entries);
      setRows(Object.entries(props.entries));
    }
  }, [props.entries]);

  const fold = (rs: [string, string][]) => {
    const out: Record<string, string> = {};
    for (const [n, t] of rs) if (n) out[n] = t;
    return out;
  };

  const commit = (next: [string, string][]) => {
    setRows(next);
    const out = fold(next);
    committed.current = JSON.stringify(out);
    props.onChange?.(out);
  };

  // Focus left the row entirely: collapse the display back to the committed
  // record, which is where empty and duplicate names disappear.
  const prune = (e: React.FocusEvent<HTMLDivElement>) => {
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    setRows((rs) => Object.entries(fold(rs)));
  };

  return (
    <div className="io-editor">
      <div className="field-label">{props.label}</div>
      {rows.length === 0 && <div className="io-empty">none declared</div>}
      {rows.map(([name, type], i) => (
        <div
          className={"io-row" + (props.marked?.has(name) ? " marked" : "")}
          key={i}
          onBlur={editable ? prune : undefined}
        >
          <input
            className="mono io-name"
            value={name}
            readOnly={!editable}
            spellCheck={false}
            onChange={(e) =>
              commit(rows.map((r, j): [string, string] => (j === i ? [e.target.value, r[1]] : r)))
            }
          />
          <span className="io-colon">:</span>
          <input
            className="mono io-type"
            value={type}
            readOnly={!editable}
            spellCheck={false}
            onChange={(e) =>
              commit(rows.map((r, j): [string, string] => (j === i ? [r[0], e.target.value] : r)))
            }
          />
          {editable && (
            <button
              className="icon-btn"
              title="Remove"
              onClick={() => commit(rows.filter((_, j) => j !== i))}
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
            while (rows.some(([name]) => name === `out${n}`)) n++;
            commit([...rows, [`out${n}`, "str"]]);
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
