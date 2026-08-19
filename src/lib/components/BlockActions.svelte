<script lang="ts">
	import { playFrom } from '$lib/audio.svelte';
	import { askAt, removeBlock, type Block } from '$lib/lesson.svelte';

	interface BlockActionsProps {
		block: Block | null;
		onClose: () => void;
	}

	let { block, onClose }: BlockActionsProps = $props();

	function run(action: () => void) {
		action();
		onClose();
	}

	/**
	 * The follow-up prompts are phrased as instructions to Praxis rather than as
	 * UI labels, because they are sent verbatim as the reader's question. "Explain
	 * more" would produce padding; naming what should deepen produces an argument.
	 */
	function deepen(instruction: string) {
		if (!block) return;
		const excerpt = block.text.slice(0, 400);
		run(() => askAt(block.id, `${instruction}\n\nThe passage in question:\n"${excerpt}"`));
	}
</script>

{#if block}
	<div
		class="scrim"
		onclick={onClose}
		onkeydown={(event) => event.key === 'Escape' && onClose()}
		role="button"
		tabindex="-1"
		aria-label="Close actions"
	></div>

	<div class="sheet" role="dialog" aria-label="Block actions">
		<p class="excerpt">{block.text.slice(0, 120)}{block.text.length > 120 ? '…' : ''}</p>

		<button onclick={() => run(() => playFrom(block.id, true))}>Play only this block</button>
		<button onclick={() => run(() => playFrom(block.id, false))}>Play from here on</button>

		<div class="divider"></div>

		<button onclick={() => deepen('Go deeper on this. What is the mechanism, and what is the strongest evidence for it?')}>
			Go deeper
		</button>
		<button onclick={() => deepen('What is the strongest objection to this, stated as its best advocate would state it?')}>
			Steelman the objection
		</button>
		<button onclick={() => deepen('Give me the concrete case behind this — a date, a ruling, a number, a named person.')}>
			Show me the case
		</button>
		<button onclick={() => deepen('How solid is this actually? Name what is verified, what is contested, and what is only recalled.')}>
			How solid is this?
		</button>

		{#if block.sources.length}
			<div class="divider"></div>
			<p class="sourcelabel">Sources</p>
			{#each block.sources as source (source.url)}
				<a class="sourcelink" href={source.url} target="_blank" rel="noreferrer noopener">
					{source.title || source.url}
				</a>
			{/each}
		{/if}

		<div class="divider"></div>
		<button class="destructive" onclick={() => run(() => removeBlock(block.id))}>
			Delete this block
		</button>
	</div>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--ink) 32%, transparent);
		z-index: 40;
		border: none;
	}
	.sheet {
		position: fixed;
		left: 0;
		right: 0;
		bottom: 0;
		z-index: 41;
		background: var(--panel);
		border-top: 1px solid var(--line);
		border-radius: 18px 18px 0 0;
		padding: 14px 16px calc(18px + var(--safe-bottom));
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 80vh;
		overflow-y: auto;
		max-width: var(--measure);
		margin: 0 auto;
	}
	.excerpt {
		margin: 4px 2px 12px;
		font-family: var(--serif);
		font-size: 14px;
		color: var(--muted);
		line-height: 1.5;
	}
	.sheet button {
		text-align: left;
		padding: 14px 10px;
		border-radius: 10px;
		font-size: 16px;
	}
	.sheet button:active {
		background: color-mix(in srgb, var(--ink) 6%, transparent);
	}
	.destructive {
		color: var(--bad);
	}
	.divider {
		height: 1px;
		background: var(--line);
		margin: 8px 0;
	}
	.sourcelabel {
		margin: 4px 10px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--muted);
	}
	.sourcelink {
		padding: 10px;
		font-size: 14px;
		color: var(--muted);
		text-decoration: none;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
