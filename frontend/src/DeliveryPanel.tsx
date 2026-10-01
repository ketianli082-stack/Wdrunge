import { DeliveryTree, type DeliveryState } from './DeliveryFiles';
import './CollaborationPanel.css';

export default function DeliveryPanel({ files, selected, onOpen }: {
  files: DeliveryState;
  selected: string | null;
  onOpen: (path: string) => void;
}) {
  return <section className="collaboration-panel delivery-panel" role="tabpanel" aria-label="交付文档">
    <header><strong>交付文档</strong><span>点击文件，在场景中预览</span></header>
    <DeliveryTree files={files} selected={selected} onOpen={onOpen} />
  </section>;
}
