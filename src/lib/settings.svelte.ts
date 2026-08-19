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

/** What an endpoint reports about one model's reasoning support. */
export interface ModelCapability {
	id: string;
	name?: string;
	context_length?: number | null;
	prompt_price?: string | null;
	supports_tools?: boolean;
	supports_reasoning?: boolean;
	supports_reasoning_effort?: boolean;
	supports_include_reasoning?: boolean;
	reasoning?: {
		mandatory?: boolean;
		default_enabled?: boolean;
		default_effort?: string;
		supported_efforts?: string[];
		supports_max_tokens?: boolean;
	} | null;
}

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

	/** Which kind of endpoint serves the model. */
	llmProvider: 'openrouter' | 'openai_compatible';
	/** Base URL of that endpoint, without a trailing slash. */
	llmBaseUrl: string;
	/** Chat model id, chosen from the endpoint's own catalogue. */
	model: string;
	/** Whisper-compatible transcription model on the same endpoint. */
	transcribeModel: string;
	/** Kokoro speech model on the same endpoint. */
	kokoroModel: string;

	/**
	 * What the chosen model says about its own reasoning, cached from the model
	 * list so the reasoning payload stays correct across reloads without
	 * refetching 400-odd models.
	 *
	 * This is the whole basis of the reasoning control. `mandatory` decides
	 * whether an off switch is even legal — turning reasoning off on a mandatory
	 * model is an HTTP 400, not a no-op. `supported_efforts` is the exact set of
	 * accepted values and it differs per model: deepseek-v4-flash takes
	 * max/high/low with no medium, gpt-5 takes high/medium/low/minimal.
	 */
	modelCapability: ModelCapability | null;

	/** How reasoning is requested. Constrained by modelCapability. */
	reasoningMode: 'off' | 'effort' | 'tokens';
	reasoningEffort: string;
	reasoningMaxTokens: number;
	/** Hide the reasoning trace in responses. Note: the tokens are still billed. */
	excludeReasoningTrace: boolean;

	/** Default number of segments offered when breaking a subject up. */
	subtopicCount: number;

	/** Speech: which provider reads the lesson aloud, and in which voice. */
	speechProvider: 'kokoro' | 'speechify';
	kokoroVoice: string;
	speechifyVoice: string;
	speechifyModel: string;
	/**
	 * SSML emotion for Speechify. Only simba-3.2 honours it, and only the five
	 * documented values — an undocumented one is accepted rather than rejected,
	 * so it would change delivery unpredictably instead of failing.
	 */
	speechifyEmotion: string;

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
	llmProvider: 'openrouter',
	llmBaseUrl: 'https://openrouter.ai/api/v1',
	model: '~deepseek/deepseek-v4-flash-latest',
	transcribeModel: 'openai/whisper-large-v3-turbo',
	kokoroModel: 'hexgrad/kokoro-82m',
	modelCapability: null,
	reasoningMode: 'off',
	reasoningEffort: '',
	reasoningMaxTokens: 2000,
	excludeReasoningTrace: true,
	subtopicCount: 5,
	speechProvider: 'kokoro',
	kokoroVoice: 'af_heart',
	speechifyVoice: 'beatrice_32',
	speechifyModel: 'simba-3.2',
	speechifyEmotion: 'energetic',
	minimumRounds: 2,
	maximumRounds: 5,
	targetBlockCount: 8,
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

let persistHook: (() => void) | null = null;

/** The vault registers here so API keys stop living in plaintext localStorage. */
export function onSettingsChange(hook: (() => void) | null) {
	persistHook = hook;
}

export function save() {
	if (!browser) return;
	const snapshot = persistHook
		? {
				...JSON.parse(JSON.stringify(settings)),
				keys: { openrouter: [], exa: [], firecrawl: [], speechify: [] }
			}
		: settings;
	localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
	persistHook?.();
}

export function hydrateSettings(payload: Partial<Settings>) {
	if (!payload || typeof payload !== 'object') return;
	const next = {
		...emptySettings,
		...payload,
		keys: { ...emptySettings.keys, ...(payload.keys || {}) },
		usage: { ...emptySettings.usage, ...(payload.usage || {}) }
	};
	Object.assign(settings, next);
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

/**
 * The reasoning parameter for the chosen model, or null to send none.
 *
 * Built from the model's own descriptor rather than from a fixed list, because
 * the wrong shape is not ignored: disabling reasoning on a model that reports
 * `mandatory: true` returns HTTP 400, and an effort value the model never
 * advertised is silently coerced to something unpredictable.
 */
export function reasoningPayload(): Record<string, unknown> | null {
	const capability = settings.modelCapability;

	// Unknown model, or one with no reasoning at all: send nothing and let the
	// endpoint do whatever it does by default.
	if (!capability || !capability.supports_reasoning) return null;

	const descriptor = capability.reasoning || {};
	const isMandatory = descriptor.mandatory === true;
	const efforts = descriptor.supported_efforts || [];

	if (settings.reasoningMode === 'off') {
		// An off switch on a mandatory model is an error, so fall back to the
		// model's own default effort — the cheapest honest thing available.
		if (!isMandatory) return { enabled: false };
		if (efforts.length) {
			return {
				effort: descriptor.default_effort || efforts[efforts.length - 1],
				exclude: settings.excludeReasoningTrace
			};
		}
		return null;
	}

	if (settings.reasoningMode === 'tokens') {
		return {
			max_tokens: Math.max(256, Math.round(settings.reasoningMaxTokens) || 2000),
			exclude: settings.excludeReasoningTrace
		};
	}

	const chosenEffort =
		settings.reasoningEffort && efforts.includes(settings.reasoningEffort)
			? settings.reasoningEffort
			: descriptor.default_effort || efforts[0];
	if (!chosenEffort) return { enabled: true };
	return { effort: chosenEffort, exclude: settings.excludeReasoningTrace };
}

/** Endpoint, models, and reasoning setting, sent with every backend request. */
export function llmPayload() {
	return {
		base_url: settings.llmBaseUrl,
		model: settings.model,
		transcribe_model: settings.transcribeModel,
		speech_model: settings.kokoroModel,
		reasoning: reasoningPayload()
	};
}

/** Effort values this model actually accepts, for the selector. */
export function availableEfforts(): string[] {
	return settings.modelCapability?.reasoning?.supported_efforts || [];
}

export function reasoningIsMandatory(): boolean {
	return settings.modelCapability?.reasoning?.mandatory === true;
}

export function modelHasReasoning(): boolean {
	return settings.modelCapability?.supports_reasoning === true;
}

export function modelSupportsTokenBudget(): boolean {
	return settings.modelCapability?.reasoning?.supports_max_tokens === true;
}

/**
 * Adopt a model and reset any reasoning choice the new model cannot honour.
 * Without this, switching from gpt-5 (medium) to deepseek-v4-flash (max/high/low)
 * would leave "medium" selected and send a value that model never advertised.
 */
export function selectModel(capability: ModelCapability) {
	settings.model = capability.id;
	settings.modelCapability = capability;

	const descriptor = capability.reasoning || {};
	const efforts = descriptor.supported_efforts || [];

	if (!capability.supports_reasoning) {
		settings.reasoningMode = 'off';
	} else if (descriptor.mandatory === true && settings.reasoningMode === 'off') {
		settings.reasoningMode = efforts.length ? 'effort' : 'tokens';
	}
	if (!efforts.includes(settings.reasoningEffort)) {
		settings.reasoningEffort = descriptor.default_effort || efforts[0] || '';
	}
	if (settings.reasoningMode === 'tokens' && descriptor.supports_max_tokens !== true) {
		settings.reasoningMode = efforts.length ? 'effort' : 'off';
	}
	save();
}

/** Switching endpoint kind resets the catalogue-derived state. */
export function selectLlmProvider(provider: 'openrouter' | 'openai_compatible') {
	settings.llmProvider = provider;
	if (provider === 'openrouter') settings.llmBaseUrl = 'https://openrouter.ai/api/v1';
	// The previous endpoint's model and its capabilities mean nothing here.
	settings.modelCapability = null;
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
