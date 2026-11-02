import { selectionLabel, type VisualAnnotation, type VisualContext } from './visualFeedback';
import './AnnotationDrafts.css';

export default function AnnotationDrafts({ items, current, disabled, onChange, onClearSelection }: {
  items: VisualAnnotation[]; current: VisualContext | null; disabled: boolean;
  onChange: (items: VisualAnnotation[]) => void; onClearSelection: () => void;
}) {
  if (!items.length && !current) return null;
  return <div className="annotation-drafts" aria-label="网页批注草稿">
    {current && <div className="annotation-current"><span>当前选区 · {selectionLabel(current)}</span>
      <button type="button" disabled={disabled} onClick={onClearSelection} aria-label="移除当前选区">×</button>
      <small>在下方输入要求即可随消息发送，也可以在网页旁添加批注。</small></div>}
    {!!items.length && <details open className="annotation-list">
      <summary>{items.length} 条批注 <small>随本条消息一起发送</small></summary>
      <div>{items.map((item, index) => <article key={item.id}>
        <header><strong><i>{index + 1}</i>{selectionLabel(item.context)}</strong>
          <button type="button" disabled={disabled} aria-label={`删除批注 ${index + 1}`} onClick={() => onChange(items.filter(value => value.id !== item.id))}>×</button></header>
        <textarea aria-label={`编辑批注 ${index + 1}`} maxLength={2000} disabled={disabled} value={item.comment}
          onChange={event => onChange(items.map(value => value.id === item.id ? { ...value, comment: event.target.value } : value))} rows={2} />
        <small title={item.context.url}>{item.context.url}</small>
      </article>)}</div>
    </details>}
  </div>;
}
