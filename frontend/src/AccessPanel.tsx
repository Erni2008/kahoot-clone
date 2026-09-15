import { useEffect, useState } from "react";
import type { AuditEvent, Collaborator } from "./types";

type Props = {
  onClose(): void;
  load(): Promise<{ collaborators: Collaborator[]; events: AuditEvent[] }>;
  create(name: string, role: "editor" | "viewer"): Promise<string>;
  revoke(id: string): Promise<void>;
};

export function AccessPanel({ onClose, load, create, revoke }: Props) {
  const [items, setItems] = useState<Collaborator[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [name, setName] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [invite, setInvite] = useState("");

  const refresh = async () => { const data = await load(); setItems(data.collaborators); setEvents(data.events); };
  useEffect(() => { void refresh(); }, []);

  const createAccess = async () => {
    const token = await create(name, role);
    setInvite(`${location.origin}/editor.html#${encodeURIComponent(token)}`);
    setName("");
    await refresh();
  };

  return <div className="sheet-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
    <aside className="sheet">
      <div className="sheet-head"><div><div className="eyebrow">Безопасность</div><h2>Команда и доступы</h2></div><button className="close" onClick={onClose}>×</button></div>
      <div className="sheet-scroll">
        <section className="access-create">
          <h3>Новый персональный доступ</h3>
          <p>У каждого человека будет отдельная ссылка. Её можно отозвать в любой момент.</p>
          <div className="access-row"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Имя человека" /><select value={role} onChange={(e) => setRole(e.target.value as "editor" | "viewer")}><option value="editor">Редактор</option><option value="viewer">Просмотр</option></select><button className="primary-button" disabled={!name.trim()} onClick={() => void createAccess()}>Создать</button></div>
          {invite && <div className="invite-box"><div><b>Ссылка готова</b><span>Она показывается только сейчас</span></div><button onClick={() => navigator.clipboard.writeText(invite)}>Копировать</button><code>{invite}</code></div>}
        </section>
        <section><div className="section-title"><div><h3>Активные доступы</h3></div><small>{items.filter((item) => !item.revoked_at).length} активных</small></div>
          <div className="people-list">{items.map((item) => <div className={`person ${item.revoked_at ? "revoked" : ""}`} key={item.id}><div className="avatar">{item.name.slice(0, 2).toUpperCase()}</div><div><b>{item.name}</b><span>{item.role === "editor" ? "Может редактировать" : "Только просмотр"} · {item.last_used_at ? `был ${new Date(item.last_used_at).toLocaleString("ru")}` : "ещё не входил"}</span></div>{!item.revoked_at && <button className="danger-button" onClick={() => void revoke(item.id).then(refresh)}>Отозвать</button>}</div>)}</div>
        </section>
        <section><div className="section-title"><div><h3>Последние изменения</h3></div></div>
          <div className="audit-list">{events.map((event) => <div key={event.id}><time>{new Date(event.created_at).toLocaleString("ru")}</time><b>{event.actor_name}</b><span>{event.action} · {event.target} {event.details}</span></div>)}</div>
        </section>
      </div>
    </aside>
  </div>;
}
