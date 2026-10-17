import { createRoot } from 'react-dom/client';
import WebsitePreview from '../src/WebsitePreview';
import { localPreviewUrl, parseSelection } from '../src/visualFeedback';

// Development-only harness: no model calls, no production route.
Object.assign(window, { feedbackTest: { localPreviewUrl, parseSelection }, feedbackRequests: [] });
document.body.style.margin = '0';
document.getElementById('root')!.style.height = '100vh';
createRoot(document.getElementById('root')!).render(<WebsitePreview busy={false} onClose={() => {}}
  onRequest={async content => { (window as unknown as { feedbackRequests: string[] }).feedbackRequests.push(content); }} />);
