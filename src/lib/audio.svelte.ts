import { absorbUsage, backendUrl, llmPayload, secretsPayload, settings } from './settings.svelte';
import { lesson, type Block } from './lesson.svelte';

/**
 * Playback, one block at a time.
 *
 * Synthesis is per block rather than per episode for two reasons. The response
 * body ceiling on Vercel is 4.5 MB and a full episode's audio would exceed it.
 * And the reader can start from any block, so whole-episode audio would be the
 * wrong unit anyway — it would have to be re-cut on every edit.
 *
 * Audio is cached by block id and by the text that produced it, so editing a
 * block invalidates only that block's audio.
 */

interface CachedAudio {
	objectUrl: string;
	/**
	 * What produced this audio. Cached clips are reused only when all of it still
	 * matches — changing the voice or the emotion has to re-synthesise just as
	 * surely as editing the words does, or the reader hears the old delivery.
	 */
	sourceText: string;
	sourceVoice: string;
	sourceEmotion: string;
	speechMarks: unknown;
}

interface PlaybackState {
	/** The block currently sounding, if any. */
	activeBlockId: string | null;
	/** True from the moment playback is requested, including while synthesizing. */
	playing: boolean;
	/** True only while waiting on the speech API. */
	synthesizing: boolean;
	/** Stop after the current block instead of continuing down the lesson. */
	singleBlockOnly: boolean;
	errorMessage: string;
}

export const playback = $state<PlaybackState>({
	activeBlockId: null,
	playing: false,
	synthesizing: false,
	singleBlockOnly: false,
	errorMessage: ''
});

const audioCache = new Map<string, CachedAudio>();
/** In-flight synthesis, keyed by block id, so prefetch and play share one request. */
const inflightByBlock = new Map<string, Promise<CachedAudio>>();
let audioElement: HTMLAudioElement | null = null;
/** Incremented on every stop so a late synthesis cannot resurrect old playback. */
let playbackGeneration = 0;

function currentVoice(): string {
	return settings.speechProvider === 'speechify'
		? settings.speechifyVoice
		: settings.kokoroVoice;
}

function cacheHits(block: Block, cached: CachedAudio | undefined, voice: string, emotion: string) {
	return Boolean(
		cached &&
			cached.sourceText === block.text &&
			cached.sourceVoice === voice &&
			cached.sourceEmotion === emotion
	);
}

async function synthesizeBlock(block: Block): Promise<CachedAudio> {
	const voiceNow = currentVoice();
	const emotionNow = settings.speechProvider === 'speechify' ? settings.speechifyEmotion : '';
	const cached = audioCache.get(block.id);
	if (cacheHits(block, cached, voiceNow, emotionNow) && cached) {
		return cached;
	}

	const pending = inflightByBlock.get(block.id);
	if (pending) return pending;

	const work = (async () => {
		const stale = audioCache.get(block.id);
		if (stale) URL.revokeObjectURL(stale.objectUrl);

		const response = await fetch(backendUrl('/api/speak'), {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				text: block.text,
				provider: settings.speechProvider,
				voice: voiceNow,
				speechify_model: settings.speechifyModel,
				emotion: settings.speechifyEmotion,
				secrets: secretsPayload(),
				llm: llmPayload()
			})
		});

		if (!response.ok) {
			let detail = `HTTP ${response.status}`;
			try {
				detail = (await response.json()).detail || detail;
			} catch {
				/* keep the status line */
			}
			throw new Error(detail);
		}

		const payload = await response.json();
		absorbUsage(payload.usage);

		const audioBytes = Uint8Array.from(atob(payload.audio_base64), (character) =>
			character.charCodeAt(0)
		);
		const objectUrl = URL.createObjectURL(
			new Blob([audioBytes], { type: payload.content_type || 'audio/mpeg' })
		);

		const entry: CachedAudio = {
			objectUrl,
			sourceText: block.text,
			sourceVoice: voiceNow,
			sourceEmotion: emotionNow,
			speechMarks: payload.speech_marks ?? null
		};
		audioCache.set(block.id, entry);
		return entry;
	})();

	inflightByBlock.set(block.id, work);
	try {
		return await work;
	} finally {
		if (inflightByBlock.get(block.id) === work) inflightByBlock.delete(block.id);
	}
}

function prefetchBlock(block: Block | undefined): void {
	if (!block) return;
	synthesizeBlock(block).catch(() => {
		/* Play will retry this block and surface the error if it still fails. */
	});
}

function playableBlocksFrom(startIndex: number): Block[] {
	return lesson.blocks.slice(startIndex).filter((block) => block.text.trim() && !block.pending);
}

function waitForEnd(element: HTMLAudioElement): Promise<'ended' | 'stopped'> {
	return new Promise((resolve) => {
		const finish = (outcome: 'ended' | 'stopped') => {
			element.onended = null;
			element.onerror = null;
			resolve(outcome);
		};
		element.onended = () => finish('ended');
		element.onerror = () => finish('stopped');
	});
}

export function stopPlayback(): void {
	playbackGeneration += 1;
	if (audioElement) {
		audioElement.pause();
		audioElement.onended = null;
		audioElement.onerror = null;
	}
	playback.playing = false;
	playback.synthesizing = false;
	playback.activeBlockId = null;
}

/**
 * Play from a block to the end of the lesson, or just that one block.
 *
 * The first block is synthesised before anything plays. While it is sounding,
 * the next block's clip is already being generated, so the join is a cache hit
 * instead of a pause on the speech API.
 */
export async function playFrom(blockId: string, singleBlockOnly = false): Promise<void> {
	stopPlayback();
	const thisGeneration = ++playbackGeneration;

	const startIndex = lesson.blocks.findIndex((block) => block.id === blockId);
	if (startIndex < 0) return;

	const queue = singleBlockOnly
		? playableBlocksFrom(startIndex).slice(0, 1)
		: playableBlocksFrom(startIndex);

	playback.playing = true;
	playback.singleBlockOnly = singleBlockOnly;
	playback.errorMessage = '';

	if (!audioElement) audioElement = new Audio();

	try {
		for (let index = 0; index < queue.length; index += 1) {
			const block = queue[index];
			if (!block) break;
			if (playbackGeneration !== thisGeneration) return;

			playback.activeBlockId = block.id;
			playback.synthesizing = !audioCache.has(block.id);
			const audio = await synthesizeBlock(block);
			playback.synthesizing = false;

			if (playbackGeneration !== thisGeneration) return;

			audioElement.src = audio.objectUrl;
			await audioElement.play();
			if (!singleBlockOnly) prefetchBlock(queue[index + 1]);
			const outcome = await waitForEnd(audioElement);
			if (outcome === 'stopped' || playbackGeneration !== thisGeneration) return;
		}
	} catch (failure) {
		playback.errorMessage = (failure as Error).message;
	} finally {
		if (playbackGeneration === thisGeneration) {
			playback.playing = false;
			playback.synthesizing = false;
			playback.activeBlockId = null;
		}
	}
}

export function isActive(blockId: string): boolean {
	return playback.activeBlockId === blockId;
}
