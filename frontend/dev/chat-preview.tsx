import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import AgentAvatar from '../src/AgentAvatar';
import MessageContent from '../src/MessageContent';
import { memberName } from '../src/TeamIdentity';
import '../src/App.css';

const delivery = `## 接口交付说明

已完成 **成员查询接口**，支持前端联调。

### 本次交付
- [x] 返回成员姓名与角色
- [x] 保留原有 Agent ID
- [ ] 前端验收

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| id | string | 成员唯一标识 |
| name | string | 姓名（角色） |

\`\`\`ts
const response = await fetch("http://localhost:8000/api/team");
const { agents } = await response.json();
\`\`\`

> 提示：调用参数继续使用英文 ID。

参考 [Markdown 文档](https://commonmark.org/help/)，字段名为 \`name\`。`;

function Preview() {
  const [read, setRead] = useState(true);
  return <main style={{ minHeight: '100vh', padding: 20, background: '#f3f3f0' }}>
    <div style={{ margin: '0 auto', width: 'min(410px, 100%)' }}>
      <button type="button" onClick={() => setRead(!read)} style={{ marginBottom: 12 }}>切换前端已读状态</button>
      <section className="chat-panel" style={{ border: '1px solid #e3e3df', borderRadius: 12, overflow: 'hidden' }}>
        <header className="panel-heading"><div><strong>团队群聊</strong><span>Markdown 交付材料 · @ 与已读状态</span></div></header>
        <div className="chat-feed" style={{ flex: 'none', overflow: 'visible' }}>
          <article className="chat-message"><AgentAvatar agent="leader" /><div className="message-body"><div className="message-meta"><strong>{memberName('leader')}</strong><time>20:10</time></div><div className="bubble"><MessageContent message={{ id: 'assign', sender: 'leader', content: '', mentions: [
            { worker: 'frontend', name: '前端开发', summary: '按 **交付说明** 完成页面联调。' },
            { worker: 'backend', name: '后端开发', summary: '整理 `GET /api/team` 的接口文档。' },
          ], readBy: read ? ['frontend', 'backend'] : ['backend'] }} /></div><div className="message-reactions"><span className="done-reaction"><span>✓</span> Done · {memberName('backend')}</span></div></div></article>
          <article className="chat-message"><AgentAvatar agent="backend" /><div className="message-body"><div className="message-meta"><strong>{memberName('backend')}</strong><time>20:12</time></div><div className="bubble"><MessageContent message={{ id: 'delivery', sender: 'backend', content: delivery }} /></div></div></article>
        </div>
      </section>
    </div>
  </main>;
}

createRoot(document.getElementById('root')!).render(<Preview />);
