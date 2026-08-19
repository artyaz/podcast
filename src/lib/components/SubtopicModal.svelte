<script lang="ts">
	import { backendUrl, llmPayload, save, secretsPayload, settings } from '$lib/settings.svelte';

	interface Subtopic {
		title: string;
		angle?: string;
	}

	interface SubtopicModalProps {
		topic: string;
		onClose: () => void;
		onProceed: (subtopics: Subtopic[]) => void;
	}

	let { topic, onClose, onProceed }: SubtopicModalProps = $props();

	let proposed = $state<Subtopic[]>([]);
	let working = $state(false);
	let message = $state('');

	async function propose() {
		working = true;
		message = '';
		try {
			const response = await fetch(backendUrl('/api/outline'), {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					topic,
					subtopic_count: settings.subtopicCount,
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
			proposed = payload.subtopics || [];
			if (proposed.length !== settings.subtopicCount) {
				// Worth saying rather than hiding: the model was asked for an exact
				// count and the spine drives how research effort gets divided.
				message = `The model returned ${proposed.length} instead of ${settings.subtopicCount}.`;
			}
		} catch (failure) {
			message = (failure as Error).message;
		} finally {
			working = false;
		}
	}

	function drop(index: number) {
		proposed.splice(index, 1);
	}
</script>

<div
	class="scrim"
	onclick={onClose}
	onkeydown={(event) => event.key === 'Escape' && onClose()}
	role="button"
	tabindex="-1"
	aria-label="Close"
></div>

<div class="sheet" role="dialog" aria-label="Break the subject into segments">
	<h2>Break it into segments</h2>
	<p class="subject">{topic}</p>

	<label>
		<span>How many segments — the subject gets divided evenly between them</span>
		<input
			type="number"
			min="2"
			max="12"
			bind:value={settings.subtopicCount}
			onchange={save}
			disabled={working}
		/>
	</label>

	<div class="row">
		<button class="ghost" onclick={propose} disabled={working}>
			{working ? 'Dividing…' : proposed.length ? 'Propose again' : 'Propose segments'}
		</button>
	</div>

	{#if message}<p class="status">{message}</p>{/if}

	{#if proposed.length}
		<ol class="list">
			{#each proposed as subtopic, index (subtopic.title + index)}
				<li>
					<div class="entry">
						<span class="title">{subtopic.title}</span>
						{#if subtopic.angle}<span class="angle">{subtopic.angle}</span>{/if}
					</div>
					<button
						class="drop"
						onclick={() => drop(index)}
						aria-label={`Remove ${subtopic.title}`}>×</button
					>
				</li>
			{/each}
		</ol>
		<p class="hint">
			Research is split across these, and the written episode follows them in this order. Drop any
			that are not worth their runtime.
		</p>
	{/if}

	<div class="actions">
		<button class="ghost" onclick={onClose}>Cancel</button>
		<button
			class="primary"
			onclick={() => onProceed(proposed)}
			disabled={!proposed.length || working}
		>
			Research these {proposed.length || ''} →
		</button>
	</div>
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--ink) 34%, transparent);
		z-index: 50;
		border: none;
	}
	.sheet {
		position: fixed;
		left: 50%;
		bottom: 0;
		transform: translateX(-50%);
		width: 100%;
		max-width: var(--measure);
		z-index: 51;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 18px 18px 0 0;
		padding: 18px 18px calc(18px + var(--safe-bottom));
		display: flex;
		flex-direction: column;
		gap: 13px;
		max-height: 88vh;
		overflow-y: auto;
	}
	h2 {
		font-family: var(--serif);
		font-size: 22px;
		margin: 0;
	}
	.subject {
		margin: 0;
		font-family: var(--serif);
		font-size: 15px;
		color: var(--muted);
		line-height: 1.5;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	label span {
		font-size: 13px;
		color: var(--muted);
		line-height: 1.45;
	}
	input {
		padding: 12px 15px;
		border: 1.5px solid var(--line);
		border-radius: 13px;
		background: var(--bg);
		color: var(--ink);
		outline: none;
		width: 100%;
	}
	.row {
		display: flex;
		gap: 9px;
	}
	.list {
		list-style: decimal;
		margin: 0;
		padding-left: 22px;
		display: flex;
		flex-direction: column;
		gap: 11px;
	}
	.list li {
		display: flex;
		align-items: flex-start;
		gap: 8px;
	}
	.entry {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 1;
		min-width: 0;
	}
	.title {
		font-size: 15px;
		line-height: 1.4;
	}
	.angle {
		font-size: 12.5px;
		color: var(--muted);
		line-height: 1.45;
	}
	.drop {
		color: var(--muted);
		font-size: 18px;
		line-height: 1;
		padding: 2px 6px;
		flex: 0 0 auto;
	}
	.hint,
	.status {
		margin: 0;
		font-size: 13px;
		color: var(--muted);
		line-height: 1.5;
	}
	.actions {
		display: flex;
		gap: 9px;
		margin-top: 4px;
	}
	.ghost {
		padding: 12px 17px;
		border-radius: 13px;
		border: 1.5px solid var(--line);
		font-size: 14px;
	}
	.ghost:disabled {
		opacity: 0.45;
	}
	.primary {
		flex: 1;
		padding: 13px 18px;
		border-radius: 13px;
		background: var(--ink);
		color: var(--bg);
		font-weight: 600;
	}
	.primary:disabled {
		opacity: 0.4;
	}
</style>
