<script lang="ts">
	import type { WorkBeat } from '$lib/lesson.svelte';

	interface ReasoningLogProps {
		beats: WorkBeat[];
		running?: boolean;
		cap?: number;
		onExpand?: () => void;
	}

	let { beats, running = false, cap = 0, onExpand }: ReasoningLogProps = $props();

	const shown = $derived(cap > 0 ? beats.slice(-cap) : beats);
	const extra = $derived(cap > 0 ? Math.max(0, beats.length - cap) : 0);
</script>

{#if shown.length}
	<ol class="beats">
		{#each shown as beat, index (index)}
			<li>
				<p class="job">– {beat.title}</p>
				<p
					class="thought"
					class:shimmering={running && index === shown.length - 1}
				>
					{beat.reasoning}
				</p>
			</li>
		{/each}
	</ol>
	{#if extra > 0 && onExpand}
		<button type="button" class="more" onclick={onExpand}>Show all reasoning</button>
	{/if}
{/if}

<style>
	.beats {
		list-style: none;
		margin: 12px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.job {
		margin: 0;
		font-size: 14px;
		font-weight: 600;
		line-height: 1.4;
	}
	.thought {
		margin: 4px 0 0;
		font-size: 14px;
		line-height: 1.5;
		color: var(--muted);
	}
	.more {
		margin-top: 10px;
		padding: 0;
		font-size: 13px;
		color: var(--muted);
		text-decoration: underline;
		text-underline-offset: 3px;
	}
</style>
