import { ConfirmModal, DialogButton, Focusable, Spinner, TextField } from '@decky/ui';
import { useEffect, useState } from 'react';

import { backend, errorMessage } from '../lib/backend';
import { shortenPath } from '../lib/format';
import type { Game, PathSuggestions, SaveCandidate } from '../lib/types';

interface Props {
  game: Game;
  closeModal?: () => void;
  onSynced: () => void;
}

const KIND_LABEL: Record<SaveCandidate['kind'], string> = {
  prefix: 'Proton prefix',
  xdg: 'Home directory',
  install: 'Install folder',
  manual: 'Manual',
};

/**
 * The manual-mapping flow from Phase 2. Candidates come from the backend's
 * heuristics; nothing is committed until the user picks one, because a wrong
 * path would push the wrong directory to their PC.
 */
export function SavePathModal({ game, closeModal, onSynced }: Props) {
  const [suggestions, setSuggestions] = useState<PathSuggestions | null>(null);
  const [selected, setSelected] = useState<string>(game.savePath ?? '');
  const [customPath, setCustomPath] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    backend
      .suggestPaths(game.appid)
      .then((result) => {
        setSuggestions(result);
        if (!game.savePath && result.candidates.length > 0) {
          setSelected(result.candidates[0].path);
        }
      })
      .catch((err) => setError(errorMessage(err)));
  }, [game.appid, game.savePath]);

  const effectivePath = customPath.trim() || selected;

  const onConfirm = async () => {
    if (!effectivePath) {
      setError('Pick a candidate or enter a path first.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const source = customPath.trim() ? 'manual' : (suggestions?.candidates.find((c) => c.path === selected)?.kind ?? 'manual');
      await backend.syncGame(game.appid, effectivePath, source);
      onSynced();
      closeModal?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ConfirmModal
      strTitle={`Sync saves: ${game.name}`}
      strOKButtonText={busy ? 'Working...' : 'Sync this folder'}
      strCancelButtonText="Cancel"
      bOKDisabled={busy || !effectivePath}
      onOK={onConfirm}
      onCancel={() => closeModal?.()}
    >
      {!suggestions && !error && <Spinner />}

      {suggestions?.hasSteamCloud && (
        <div style={{ marginBottom: '8px', color: '#e3b341' }}>
          This game already uses Steam Cloud. Syncing it here too may cause conflicting writes.
        </div>
      )}

      {suggestions && suggestions.candidates.length === 0 && (
        <div style={{ marginBottom: '8px' }}>
          No likely save folder found. Enter the path manually; it is remembered for next time.
        </div>
      )}

      <Focusable style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '12px' }}>
        {suggestions?.candidates.map((candidate) => (
          <DialogButton
            key={candidate.path}
            onClick={() => {
              setSelected(candidate.path);
              setCustomPath('');
            }}
            style={{
              textAlign: 'left',
              padding: '8px',
              background: candidate.path === effectivePath ? 'rgba(88, 166, 255, 0.25)' : undefined,
            }}
          >
            <div style={{ fontSize: '14px' }}>{candidate.label}</div>
            <div style={{ fontSize: '11px', opacity: 0.6 }}>
              {KIND_LABEL[candidate.kind]} · {candidate.entryCount} item
              {candidate.entryCount === 1 ? '' : 's'} · {shortenPath(candidate.path)}
            </div>
          </DialogButton>
        ))}
      </Focusable>

      <TextField
        label="Or enter a path manually"
        description={`e.g. ${suggestions?.prefixRoot ?? suggestions?.installPath ?? '/home/deck/...'}`}
        value={customPath}
        onChange={(event) => setCustomPath(event.target.value)}
      />

      {error && <div style={{ marginTop: '8px', color: '#ff7b72' }}>{error}</div>}
    </ConfirmModal>
  );
}
