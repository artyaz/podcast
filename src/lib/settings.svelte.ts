import { browser } from '$app/environment';

/**
 * Everything configurable, stored only in this browser.
 *
 * No key is ever baked into the deployment. Keys live in localStorage, travel
 * with each request, and the backend hands back updated usage counters which
 * get stored here so rotation survives across requests — the backend is
 * stateless, so this file is the only memory rotation has.
 */

export type ProviderName = 'openrouter' | 'exa' | 'firecrawl' | 'speechify';

export const PROVIDER_NAMES: ProviderName[] = ['openrouter', 'exa', 'firecrawl', 'speechify'];

export interface KeyUsageRecord {
	spent_dollars?: number;
	calls?: number;
	characters?: number;
	remaining_credits?: number | null;
}

export interface Settings {
	/** Several keys per provider, rotated by remaining quota. One per line in the UI. */
	keys: Record<ProviderName, string[]>;
	/** Per-key counters returned by the backend, addressed by key fingerprint. */
	usage: Record<ProviderName, Record<string, KeyUsageRecord>>;

	/** Chat model on OpenRouter. Non-reasoning variant by default. */
	model: string;

	/** Speech: which provider reads the lesson aloud, and in which voice. */
	speechProvider: 'kokoro' | 'speechify';
	kokoroVoice: string;
	speechifyVoice: string;
	speechifyModel: string;

	/** How hard the research loop works before it is allowed to write. */
	minimumRounds: number;
	maximumRounds: number;
	targetBlockCount: number;

	/**
	 * Seconds granted per invocation. Hobby kills a function at 300s, and a round
	 * that gets cut off still spends a turn writing up what it found — measured
	 * overrunning by roughly 30 seconds. 200 leaves room for that plus the response.
	 */
	budgetSeconds: number;

	/**
	 * Where the Python backend lives. Empty means same origin, which is the
	 * single-project deployment. Set it to the backend's URL if the Python
	 * service is deployed as its own Vercel project.
	 */
	backendBaseUrl: string;
}

const STORAGE_KEY = 'praxis.settings';

const emptySettings: Settings = {
	keys: { openrouter: [], exa: [], firecrawl: [], speechify: [] },
	usage: { openrouter: {}, exa: {}, firecrawl: {}, speechify: {} },
	model: '~deepseek/deepseek-v4-flash-latest',
	speechProvider: 'kokoro',
	kokoroVoice: 'af_heart',
	speechifyVoice: 'alec',
	speechifyModel: 'simba-3.0',
	minimumRounds: 2,
	maximumRounds: 5,
	targetBlockCount: 12,
	budgetSeconds: 200,
	backendBaseUrl: ''
};

function load(): Settings {
	if (!browser) return structuredClone(emptySettings);
	try {
		const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
		return {
			...emptySettings,
			...raw,
			keys: { ...emptySettings.keys, ...(raw.keys || {}) },
			usage: { ...emptySettings.usage, ...(raw.usage || {}) }
		};
	} catch {
		return structuredClone(emptySettings);
	}
}

export const settings = $state<Settings>(load());

export function save() {
	if (!browser) return;
	localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
}

/** Parse a textarea of keys, one per line, dropping blanks and duplicates. */
export function setKeysFromText(provider: ProviderName, text: string) {
	const parsed: string[] = [];
	for (const line of text.split('\n')) {
		const trimmed = line.trim();
		if (trimmed && !parsed.includes(trimmed)) parsed.push(trimmed);
	}
	settings.keys[provider] = parsed;
	save();
}

export function keysAsText(provider: ProviderName): string {
	return (settings.keys[provider] || []).join('\n');
}

export function hasKey(provider: ProviderName): boolean {
	return (settings.keys[provider] || []).length > 0;
}

/** The minimum needed to research anything at all. */
export function canResearch(): boolean {
	return hasKey('openrouter') && hasKey('exa');
}

export function canSpeak(): boolean {
	return settings.speechProvider === 'speechify' ? hasKey('speechify') : hasKey('openrouter');
}

/** The secrets envelope sent with every backend request. */
export function secretsPayload() {
	return { keys: settings.keys, usage: settings.usage };
}

/** Store the counters the backend returned, so the next request rotates correctly. */
export function absorbUsage(returnedUsage: unknown) {
	if (!returnedUsage || typeof returnedUsage !== 'object') return;
	for (const provider of PROVIDER_NAMES) {
		const providerUsage = (returnedUsage as Record<string, unknown>)[provider];
		if (providerUsage && typeof providerUsage === 'object') {
			settings.usage[provider] = providerUsage as Record<string, KeyUsageRecord>;
		}
	}
	save();
}

export function backendUrl(path: string): string {
	const base = (settings.backendBaseUrl || '').replace(/\/+$/, '');
	return base ? `${base}${path}` : path;
}

/** A human-readable fingerprint matching the backend's, for the usage table. */
export function fingerprintOf(apiKey: string): string {
	if (apiKey.length <= 12) return `key-${apiKey.length}`;
	return `${apiKey.slice(0, 6)}...${apiKey.slice(-4)}`;
}

/** Portable config blob so a phone and a laptop can share a setup. */
export function exportConfig(): string {
	return `praxis:${btoa(unescape(encodeURIComponent(JSON.stringify(settings))))}`;
}

export function importConfig(text: string): boolean {
	try {
		const decoded = JSON.parse(
			decodeURIComponent(escape(atob(text.trim().replace(/^praxis:/, ''))))
		);
		Object.assign(settings, { ...emptySettings, ...decoded });
		settings.keys = { ...emptySettings.keys, ...(decoded.keys || {}) };
		settings.usage = { ...emptySettings.usage, ...(decoded.usage || {}) };
		save();
		return true;
	} catch {
		return false;
	}
}
