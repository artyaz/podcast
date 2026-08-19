<script lang="ts">
	import { askAt } from '$lib/lesson.svelte';
	import { hasKey } from '$lib/settings.svelte';
	import { startRecording as startMic, stopRecording as stopMic, transcribeBlob } from '$lib/voice';

	interface AskBarProps {
		/** The block the reader last touched; the answer is inserted after it. */
		anchorBlockId: string | null;
	}

	let { anchorBlockId }: AskBarProps = $props();

	let typedQuestion = $state('');
	let recording = $state(false);
	let transcribing = $state(false);
	let statusMessage = $state('');

	/**
	 * The bar sits above the on-screen keyboard rather than behind it.
	 *
	 * On iOS the layout viewport does not shrink when the keyboard opens, so
	 * `position: fixed; bottom: 0` puts this control underneath the keys. The
	 * visual viewport does report the covered height, so the offset is taken from
	 * there. Desktop browsers report no gap and the value stays zero.
	 */
	let keyboardOffset = $state(0);

	$effect(() => {
		const visualViewport = window.visualViewport;
		if (!visualViewport) return;

		const recompute = () => {
			const covered =
				window.innerHeight - visualViewport.height - visualViewport.offsetTop;
			keyboardOffset = Math.max(0, Math.round(covered));
		};

		recompute();
		visualViewport.addEventListener('resize', recompute);
		visualViewport.addEventListener('scroll', recompute);
		return () => {
			visualViewport.removeEventListener('resize', recompute);
			visualViewport.removeEventListener('scroll', recompute);
		};
	});

	async function submitTyped() {
		const question = typedQuestion.trim();
		if (!question) return;
		typedQuestion = '';
		await askAt(anchorBlockId, question);
	}

	async function startRecording() {
		statusMessage = '';
		try {
			await startMic();
			recording = true;
		} catch (failure) {
			statusMessage = `Microphone unavailable: ${(failure as Error).message}`;
		}
	}

	async function stopRecording() {
		recording = false;
		transcribing = true;
		statusMessage = 'Transcribing…';
		try {
			const blob = await stopMic();
			const spokenQuestion = await transcribeBlob(blob);
			statusMessage = '';
			await askAt(anchorBlockId, spokenQuestion);
		} catch (failure) {
			statusMessage = (failure as Error).message;
		} finally {
			transcribing = false;
		}
	}
</script>

<div class="bar" style="transform: translateY(-{keyboardOffset}px)">
	{#if statusMessage}
		<p class="status">{statusMessage}</p>
	{/if}

	<div class="row">
		<input
			bind:value={typedQuestion}
			onkeydown={(event) => event.key === 'Enter' && submitTyped()}
			placeholder="Ask anything, right here…"
			autocapitalize="sentences"
			autocomplete="off"
			spellcheck="false"
			disabled={transcribing}
		/>

		{#if typedQuestion.trim()}
			<button class="send" onclick={submitTyped} aria-label="Send question">↑</button>
		{:else}
			<button
				class="mic"
				class:recording
				onclick={recording ? stopRecording : startRecording}
				disabled={transcribing || !hasKey('openrouter')}
				aria-label={recording ? 'Stop recording' : 'Ask by voice'}
				title={hasKey('openrouter') ? 'Ask by voice' : 'Add an OpenRouter key in Settings'}
			>
				{#if transcribing}
					<span class="spinner"></span>
				{:else if recording}
					◼
				{:else}
					●
				{/if}
			</button>
		{/if}
	</div>
</div>

<style>
	.bar {
		position: fixed;
		left: 0;
		right: 0;
		bottom: 0;
		z-index: 30;
		padding: 10px 16px calc(10px + var(--safe-bottom));
		background: linear-gradient(transparent, var(--bg) 42%);
		transition: transform 0.16s ease-out;
	}
	.row {
		display: flex;
		gap: 8px;
		max-width: var(--measure);
		margin: 0 auto;
	}
	input {
		flex: 1;
		min-width: 0;
		padding: 13px 16px;
		border: 1.5px solid var(--line);
		border-radius: 999px;
		background: var(--panel);
		color: var(--ink);
		outline: none;
	}
	input:focus {
		border-color: color-mix(in srgb, var(--ink) 45%, var(--line));
	}
	.send,
	.mic {
		width: 46px;
		height: 46px;
		flex: 0 0 46px;
		border-radius: 50%;
		border: 1.5px solid var(--line);
		background: var(--panel);
		font-size: 15px;
		line-height: 1;
	}
	.send {
		background: var(--ink);
		color: var(--bg);
		border-color: var(--ink);
		font-size: 19px;
	}
	.mic.recording {
		border-color: var(--bad);
		color: var(--bad);
	}
	.mic:disabled {
		opacity: 0.4;
	}
	.status {
		max-width: var(--measure);
		margin: 0 auto 8px;
		font-size: 13px;
		color: var(--muted);
	}
	.spinner {
		display: inline-block;
		width: 12px;
		height: 12px;
		border: 1.5px solid var(--line);
		border-top-color: var(--ink);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>
