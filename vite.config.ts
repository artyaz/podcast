import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		// `vercel dev` serves the Python function; plain `vite dev` proxies to it.
		proxy: {
			'/api': {
				target: process.env.PRAXIS_BACKEND_URL || 'http://127.0.0.1:8000',
				changeOrigin: true
			}
		}
	}
});
