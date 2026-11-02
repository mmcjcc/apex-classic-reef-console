// Test environment for vitest + jsdom. jsdom lacks a few browser features the component library expects.
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => cleanup());

// jsdom's getComputedStyle rejects a second argument; the component library passes one
const { getComputedStyle } = window;
window.getComputedStyle = (element) => getComputedStyle(element);

window.HTMLElement.prototype.scrollIntoView = () => undefined;

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

// self-sizing text boxes listen for web fonts finishing loading; jsdom has no document.fonts
if (!('fonts' in document)) {
  Object.defineProperty(document, 'fonts', {
    configurable: true,
    value: { addEventListener: () => undefined, removeEventListener: () => undefined },
  });
}

class ResizeObserverStub implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
window.ResizeObserver = ResizeObserverStub;
