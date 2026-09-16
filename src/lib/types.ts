/** Every backend call returns this envelope; see _ok/_err in main.py. */
export interface BackendResult<T> {
  ok: boolean;
  data?: T;
  error?: BackendError;
}

export interface BackendError {
  code: string;
  message: string;
  status?: number;
  body?: string;
}

export interface SyncthingDevice {
  deviceId: string;
  name: string;
  isLocal: boolean;
}

export interface DaemonStatus {
  running: boolean;
  canManage: boolean;
  command?: string[] | null;
  unitInstalled?: boolean;
  autostart?: boolean;
  unitActive?: boolean;
  /** Running only because a desktop session started it; dies when you leave Desktop Mode. */
  sessionScoped?: boolean;
  error?: string;
}

export interface RemoteStatus {
  configured: boolean;
  connected?: boolean;
  baseUrl?: string;
  name?: string;
  deviceId?: string;
  os?: string;
  version?: string;
  /** True when SyncDeck can create folders on the PC with a mapped path (Windows only). */
  autoAdd?: boolean;
  error?: BackendError;
}

/** What happened on the PC when a folder was created on the Deck. */
export interface RemoteAddResult {
  added: boolean;
  reason: 'not_configured' | 'not_windows' | 'exists' | 'created' | 'error';
  pcPath?: string | null;
  /** mapped: exact Windows path; install_found: located in the PC's Steam library; default: ~\SyncDeck\<game>. */
  how?: 'mapped' | 'install_found' | 'default' | 'kept';
  /** The directory already existed on the PC, so existing saves will merge. */
  pcExisting?: boolean;
  error?: BackendError;
}

export interface ConnectionStatus {
  connected: boolean;
  daemon?: DaemonStatus;
  remote?: RemoteStatus;
  endpoint?: {
    baseUrl: string;
    hasApiKey: boolean;
    configPath: string | null;
    source: 'discovered' | 'manual';
  };
  version?: string;
  myDeviceId?: string;
  devices?: SyncthingDevice[];
  targetDevices?: string[];
  error?: BackendError;
  portConflict?: {
    port: number;
    owner: string;
    message: string;
  } | null;
}

export interface Game {
  appid: number;
  name: string;
  installDir: string;
  installPath: string;
  libraryPath: string;
  lastUpdated: number;
  sizeOnDisk: number;
  isProton: boolean;
  folderId: string;
  savePath: string | null;
  pathSource: string | null;
  synced: boolean;
  ignored: boolean;
  hasSteamCloud: boolean;
  cloudSource: 'appinfo' | 'remotecache' | 'none';
  /** True when the Syncthing folder was created outside SyncDeck and recognized by path. */
  adopted: boolean;
  /** Best save-folder guess for an unmapped game; null when nothing was found. */
  detected: SaveCandidate | null;
  /** False when the game has never been launched here (no Proton prefix yet). */
  playedOnDeck: boolean;
  /** The mapped save path no longer exists (moved, or on a drive that is out). */
  pathMissing?: boolean;
  /** Where the saves appear to be now, when pathMissing and a candidate was found. */
  relocateTo?: SaveCandidate | null;
  library?: { path: string; contentId: string; label: string; mounted: boolean } | null;
  /** The same folder on a Windows PC, e.g. ~\Documents\Eidos\..., when it can be mapped. */
  pcPath?: string | null;
}

export type UnavailableReason = 'library_missing' | 'uninstalled' | 'moved' | 'game_missing';

/** A mapped game whose saves cannot be reached right now. */
export interface UnavailableGame {
  appid: number;
  name: string;
  savePath: string;
  folderId: string;
  adopted: boolean;
  reason: UnavailableReason;
  /** Label of the library involved, e.g. an SD card name. */
  libraryLabel: string | null;
  /** True when that library is known to Steam but not currently mounted. */
  driveOut?: boolean;
  installed: boolean;
  relocateTo: SaveCandidate | null;
}

export interface GameListResult {
  games: Game[];
  /** Games hidden because Steam Cloud already syncs them. */
  hiddenCloud: number;
  skipCloudSaves: boolean;
  total: number;
  unavailable: UnavailableGame[];
}

export interface SaveCandidate {
  path: string;
  label: string;
  kind: 'prefix' | 'xdg' | 'install' | 'manual';
  confidence: number;
  entryCount: number;
  pcPath?: string | null;
}

export interface PathSuggestions {
  appid: number;
  name: string;
  current: string | null;
  hasSteamCloud: boolean;
  prefixRoot: string | null;
  installPath: string;
  candidates: SaveCandidate[];
}

export interface FolderStatus {
  state: string;
  /** Remote device names that have accepted the shared folder. */
  accepted?: string[];
  /** Remote device names that still need to approve the folder in their Syncthing. */
  awaitingAccept?: string[];
  /** Syncthing's folder-level error, e.g. "folder path missing" after an SD card is removed. */
  folderError?: string | null;
  stateChanged?: string;
  needItems: number;
  globalBytes: number;
  localBytes: number;
  errors: number;
  pullErrors: number;
  scanning: boolean;
  inSync: boolean;
  error?: BackendError;
}

export type StatusMap = Record<string, FolderStatus>;

export interface LibraryChange {
  changed: boolean;
  added: number[];
  removed: number[];
  newUnmapped: Array<{ appid: number; name: string }>;
}
