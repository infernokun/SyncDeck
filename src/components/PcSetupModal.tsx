import { ConfirmModal, TextField } from '@decky/ui';
import { useState } from 'react';

import { backend, errorMessage } from '../lib/backend';
import type { RemoteStatus } from '../lib/types';

interface Props {
  current?: RemoteStatus;
  closeModal?: () => void;
  onSaved: () => void;
}

/**
 * Connect SyncDeck to the PC's Syncthing API so new folders are created
 * there with the matching Windows path, instead of waiting to be accepted.
 */
export function PcSetupModal({ current, closeModal, onSaved }: Props) {
  const [baseUrl, setBaseUrl] = useState(current?.baseUrl ?? '');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const status = await backend.setRemoteConfig(baseUrl, apiKey);
      if (status.configured && !status.connected) {
        setError(status.error?.message ?? 'Could not reach the PC. Check the address and that its GUI listens on the network.');
        return;
      }
      onSaved();
      closeModal?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ConfirmModal
      strTitle="PC Syncthing"
      strOKButtonText={busy ? 'Checking...' : baseUrl ? 'Save' : 'Remove'}
      strCancelButtonText="Cancel"
      bOKDisabled={busy}
      onOK={save}
      onCancel={() => closeModal?.()}
    >
      <div style={{ fontSize: '12px', opacity: 0.8, marginBottom: '10px' }}>
        On the PC, open Syncthing, go to Actions, Settings, GUI. Set the listen address to 0.0.0.0:8384 and copy the
        API key. Then folders SyncDeck creates are added on the PC at the matching Windows path, and nothing needs
        accepting.
      </div>
      <TextField
        label="PC Syncthing address"
        description="e.g. https://10.0.0.5:8384 (use http:// if the PC's GUI has TLS off)"
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
      />
      <TextField
        label="API key"
        description={current?.configured ? 'Leave empty to keep the saved key' : 'From the PC Syncthing GUI settings'}
        bIsPassword
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
      />
      {error && <div style={{ marginTop: '8px', color: '#ff7b72' }}>{error}</div>}
    </ConfirmModal>
  );
}
