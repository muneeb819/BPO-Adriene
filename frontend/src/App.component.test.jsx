import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

/**
 * Component and WebSocket coverage for the console.
 *
 * The pure-function tests in App.test.jsx cover what a reply sounds like; these
 * cover whether the thing actually runs: that the call connects, that a typed
 * utterance reaches the socket, and that the server's replies land in the
 * transcript. That path had no test at all before this file.
 */

// --------------------------------------------------------------------------
// doubles. The console talks to exactly two things: fetch and WebSocket.
// --------------------------------------------------------------------------

function jsonResponse(body) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
}

/** Answers every endpoint the console calls on mount, with plausible shapes. */
function installFetch() {
  const fetchMock = vi.fn((url) => {
    const path = String(url);
    if (path.includes('/api/tts/voices')) return jsonResponse({ installed: false, voices: [] });
    if (path.includes('/api/config')) return jsonResponse({ mode: 'streaming', voice: 'af_heart' });
    if (path.includes('/api/brain/status')) return jsonResponse({ enabled: true, learned: 0 });
    if (path.includes('/api/calls')) return jsonResponse([]);
    if (path.includes('/api/call/start')) {
      return jsonResponse({ session_id: 'sess-1', bot_prompt: 'Hi, this is Adriene.' });
    }
    return jsonResponse({});
  });
  global.fetch = fetchMock;
  return fetchMock;
}

/** A WebSocket stand-in that records what the app sends and can push replies. */
class FakeSocket {
  static instances = [];
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    this.onmessage = null;
    this.onclose = null;
    FakeSocket.instances.push(this);
  }

  send(payload) {
    this.sent.push(payload);
  }

  /** Deliver a server frame the way the real socket would. */
  receive(obj) {
    this.onmessage({ data: JSON.stringify(obj) });
  }

  close() {
    this.readyState = 3;
    if (this.onclose) this.onclose();
  }
}

beforeEach(() => {
  FakeSocket.instances = [];
  global.WebSocket = FakeSocket;
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
});

const startCall = async (user) => {
  await user.click(screen.getByRole('button', { name: /start mock call/i }));
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  return FakeSocket.instances[0];
};

// --------------------------------------------------------------------------

describe('the console', () => {
  it('renders without a backend or a microphone', async () => {
    render(<App />);
    expect(await screen.findByRole('button', { name: /start mock call/i })).toBeInTheDocument();
  });

  it('tells the user to start a call before typing, instead of silently dropping it', async () => {
    // The guard matters: a typed reply before the socket exists used to be a
    // no-op, which reads as the app being broken.
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole('textbox'), 'hello?');
    await user.click(screen.getByRole('button', { name: /^send$/i }));

    expect(await screen.findByText(/call not open/i)).toBeInTheDocument();
  });
});

describe('starting a call', () => {
  it('opens a socket for the session and shows the opening line', async () => {
    const user = userEvent.setup();
    render(<App />);
    const socket = await startCall(user);

    expect(socket.url).toMatch(/\/ws\/call\/sess-1$/);
    expect(await screen.findByText('Hi, this is Adriene.')).toBeInTheDocument();
  });

  it('sends a typed utterance to the socket as a user_utterance frame', async () => {
    const user = userEvent.setup();
    render(<App />);
    const socket = await startCall(user);

    const box = await screen.findByRole('textbox');
    await user.type(box, 'is this a scam?');
    await user.click(screen.getByRole('button', { name: /^send$/i }));

    await waitFor(() => expect(socket.sent.length).toBeGreaterThan(0));
    const frame = JSON.parse(socket.sent[0]);
    expect(frame).toEqual({ type: 'user_utterance', text: 'is this a scam?' });
  });

  it('clears the input after sending, so a reply is not sent twice by accident', async () => {
    const user = userEvent.setup();
    render(<App />);
    await startCall(user);

    const box = await screen.findByRole('textbox');
    await user.type(box, 'yes');
    await user.click(screen.getByRole('button', { name: /^send$/i }));

    await waitFor(() => expect(box.value).toBe(''));
  });
});

describe('server frames', () => {
  it('appends a bot_reply to the transcript', async () => {
    const user = userEvent.setup();
    render(<App />);
    const socket = await startCall(user);

    socket.receive({ type: 'bot_reply', text: 'Take your time — no rush.' });
    expect(await screen.findByText('Take your time — no rush.')).toBeInTheDocument();
  });

  it('shows a transfer as its own line rather than dropping it', async () => {
    // A transfer is the highest-stakes outcome in the product; if the frame is
    // ignored the operator never learns the call was handed to a human.
    const user = userEvent.setup();
    render(<App />);
    const socket = await startCall(user);

    socket.receive({ type: 'transfer' });
    expect(await screen.findByText(/transfer triggered/i)).toBeInTheDocument();
  });

  it('surfaces a server error instead of leaving the user waiting', async () => {
    const user = userEvent.setup();
    render(<App />);
    const socket = await startCall(user);

    socket.receive({ type: 'error', message: 'brain offline' });
    expect(await screen.findByText(/brain offline/i)).toBeInTheDocument();
  });
});
