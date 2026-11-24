import { createRoot } from 'react-dom/client';
import { useCallback, useState } from 'react';
import WebsitePreview from '../src/WebsitePreview';
import { annotationRequest, localPreviewUrl, parseSelection, type VisualAnnotation, type VisualContext } from '../src/visualFeedback';

// Development-only harness: no model calls, no production route.
Object.assign(window, { feedbackTest: { localPreviewUrl, parseSelection, annotationRequest }, feedbackRequests: [], feedbackAnnotations: [], feedbackSelection: null, feedbackSelectionVersion: 0 });
document.body.style.margin = '0';
document.getElementById('root')!.style.height = '100vh';
function Harness() {
  const [items, setItems] = useState<VisualAnnotation[]>([]);
  const observeSelection = useCallback((selection: VisualContext | null) => {
    const state = window as unknown as { feedbackSelectionVersion: number };
    Object.assign(window, { feedbackSelection: selection, feedbackSelectionVersion: state.feedbackSelectionVersion + 1 });
  }, []);
  return <WebsitePreview busy={false} annotations={items} onClose={() => {}}
    onSelection={observeSelection}
    onAnnotation={item => setItems(current => {
      const next = [...current, item]; Object.assign(window, { feedbackAnnotations: next }); return next;
    })}
    onRequest={async content => { (window as unknown as { feedbackRequests: string[] }).feedbackRequests.push(content); }} />;
}
createRoot(document.getElementById('root')!).render(<Harness />);
