import { useAvatar } from "../hooks/useAvatar";
import { agentColor, avatarInitial } from "../lib/avatar";
import { getServerAvatars, listPresets, presetUrl } from "../lib/avatarStore";
import { fileToAvatarDataUrl } from "../lib/image";
import { useLocale, t } from "../lib/locale";
import {
  deleteServerAvatar,
  putServerAvatar,
} from "../lib/serverAvatars";
import { message, React } from "../lib/ui";

interface AvatarEditorProps {
  agentId: string;
  agentName: string;
}

export function AvatarEditor({ agentId, agentName }: AvatarEditorProps) {
  const locale = useLocale();
  const current = useAvatar(agentId);
  const stored = getServerAvatars()[agentId];
  const fileRef = React.useRef<HTMLInputElement>(null);
  const presets = listPresets(locale);
  const color = agentColor(agentId);
  const [busy, setBusy] = React.useState(false);

  const guard = async (action: () => Promise<void>, ok: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await action();
      message.success(ok);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      message.error(
        t(locale, "avatarFailed", {
          reason: reason || t(locale, "avatarFailedRetry"),
        }),
      );
    } finally {
      setBusy(false);
    }
  };

  const onPreset = (key: string) => {
    const url = presetUrl(key);
    if (!url) return;
    void guard(
      () => putServerAvatar(agentId, url),
      t(locale, "avatarUpdated"),
    );
  };

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      message.error(t(locale, "selectImage"));
      return;
    }
    const dataUrl = await fileToAvatarDataUrl(file, 256).catch(() => null);
    if (!dataUrl) {
      message.error(t(locale, "imageReadFailed"));
      return;
    }
    void guard(
      () => putServerAvatar(agentId, dataUrl),
      t(locale, "avatarUpdated"),
    );
  };

  const onReset = () => {
    void guard(
      () => deleteServerAvatar(agentId),
      t(locale, "avatarReset"),
    );
  };

  const currentStyle: React.CSSProperties = current
    ? { backgroundImage: `url(${current})` }
    : { background: color };

  return (
    <div className={"ao-ava-editor" + (busy ? " is-busy" : "")}>
      <div className="ao-ava-current" style={currentStyle}>
        {current ? "" : avatarInitial(agentName)}
      </div>

      <div className="ao-ava-side">
        <div className="ao-ava-presets">
          {presets.map((p) => (
            <button
              key={p.key}
              type="button"
              title={p.label}
              disabled={busy}
              className={
                "ao-ava-preset" + (stored === p.url ? " is-active" : "")
              }
              style={{ backgroundImage: `url(${p.url})` }}
              onClick={() => onPreset(p.key)}
            />
          ))}
        </div>

        <div className="ao-ava-actions">
          <button
            className="ao-btn"
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
          >
            {t(locale, "uploadAvatar")}
          </button>
          <button
            className="ao-btn"
            type="button"
            disabled={busy}
            onClick={onReset}
          >
            {t(locale, "resetAvatar")}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            onChange={onUpload}
          />
        </div>
        <p className="ao-ava-hint">{t(locale, "avatarHint")}</p>
      </div>
    </div>
  );
}
