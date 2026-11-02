import { describe, expect, it } from 'vitest';
import { pickVoice, scoreVoice, splitSentences } from './App';

/**
 * The console speaks replies one sentence at a time so playback can start
 * before the next chunk is synthesised. That makes splitSentences load-bearing:
 * if it drops text or splits mid-sentence, the customer hears a reply that is
 * cut apart or truncated.
 */
describe('splitSentences', () => {
  it('splits on terminal punctuation and keeps the punctuation', () => {
    expect(splitSentences('Hello there. How are you? Fine!')).toEqual([
      'Hello there.',
      'How are you?',
      'Fine!',
    ]);
  });

  it('treats an ellipsis as a sentence end', () => {
    expect(splitSentences('Well… let me check.')).toEqual(['Well…', 'let me check.']);
  });

  it('keeps a decimal number inside one sentence', () => {
    // Regression: the engine read "The balance is 1." and "5 million dollars."
    // as two utterances, so the customer heard the figure in two halves.
    expect(splitSentences('The balance is 1.5 million dollars.')).toEqual([
      'The balance is 1.5 million dollars.',
    ]);
  });

  it('handles several decimals in one reply', () => {
    expect(splitSentences('Rate is 3.25 percent and the fee is 0.75 percent.')).toEqual([
      'Rate is 3.25 percent and the fee is 0.75 percent.',
    ]);
  });

  it('still ends a sentence on a full stop that follows a digit', () => {
    // The guard must not swallow a real terminator: "due in 5. Next step."
    expect(splitSentences('Payment is due in 5. Call us then.')).toEqual([
      'Payment is due in 5.',
      'Call us then.',
    ]);
  });

  it('returns a single chunk when there is no punctuation at all', () => {
    expect(splitSentences('no punctuation here')).toEqual(['no punctuation here']);
  });

  it('trims whitespace around each chunk', () => {
    expect(splitSentences('  First.   Second.  ')).toEqual(['First.', 'Second.']);
  });

  it('returns nothing for empty or whitespace-only input', () => {
    expect(splitSentences('')).toEqual([]);
    expect(splitSentences('   ')).toEqual([]);
  });

  it('does not lose or reorder any characters across the whole text', () => {
    // Guards against a regex change quietly swallowing punctuation or words.
    const text = 'Good morning. Your account is past due by 1.5 months. Shall I take a payment?';
    expect(splitSentences(text).join(' ')).toBe(text);
  });
});

/**
 * scoreVoice decides which browser voice the fallback engine prefers. It only
 * matters when the neural engine is unavailable, which is exactly when nobody
 * is watching — so it is asserted rather than assumed.
 */
describe('scoreVoice', () => {
  it('prefers a voice that advertises itself as natural or neural', () => {
    const neural = scoreVoice({ name: 'Microsoft Aria Online (Natural)', lang: 'en-US' });
    const plain = scoreVoice({ name: 'Some Voice', lang: 'en-US' });
    expect(neural).toBeGreaterThan(plain);
  });

  it('prefers a named female voice over a generic one', () => {
    expect(scoreVoice({ name: 'Samantha', lang: 'en-US' }))
      .toBeGreaterThan(scoreVoice({ name: 'Generic', lang: 'en-US' }));
  });

  it('penalises the obviously robotic desktop voices', () => {
    expect(scoreVoice({ name: 'David', lang: 'en-US' }))
      .toBeLessThan(scoreVoice({ name: 'Generic', lang: 'en-US' }));
    expect(scoreVoice({ name: 'eSpeak Compact', lang: 'en-US' }))
      .toBeLessThan(scoreVoice({ name: 'Generic', lang: 'en-US' }));
  });

  it('rejects a non-English voice outright', () => {
    // -10 for the language outweighs every bonus above, so a German voice can
    // never outrank an English one even if it is "Natural".
    expect(scoreVoice({ name: 'Natural Female', lang: 'de-DE' }))
      .toBeLessThan(scoreVoice({ name: 'Plain', lang: 'en-US' }));
  });

  it('tolerates a missing name or lang', () => {
    expect(() => scoreVoice({})).not.toThrow();
    expect(scoreVoice({})).toBeLessThan(scoreVoice({ name: 'Samantha', lang: 'en-US' }));
  });
});

/**
 * pickVoice is what stands between a saved preference and the voice actually
 * used on the next call.
 */
describe('pickVoice', () => {
  const voices = [
    { name: 'David', lang: 'en-US' },
    { name: 'Samantha', lang: 'en-US' },
    { name: 'Anna', lang: 'de-DE' },
  ];

  it('returns null instead of throwing when the browser exposes no voices', () => {
    expect(pickVoice([], null)).toBeNull();
    expect(pickVoice(undefined, null)).toBeNull();
    expect(pickVoice(null, 'Samantha')).toBeNull();
  });

  it('honours a saved voice when it is still installed', () => {
    expect(pickVoice(voices, 'Samantha')).toEqual({ name: 'Samantha', lang: 'en-US' });
  });

  it('falls back to the best remaining voice when the saved one is gone', () => {
    // A saved voice can disappear when the OS updates; the call must not fail.
    expect(pickVoice(voices, 'Uninstalled Voice')).toEqual({ name: 'Samantha', lang: 'en-US' });
  });

  it('picks the highest scoring voice when nothing is saved', () => {
    expect(pickVoice(voices, null).name).toBe('Samantha');
  });

  it('never selects a non-English voice by accident', () => {
    const withLoudGerman = [
      { name: 'Natural Female Anna', lang: 'de-DE' },
      { name: 'Plain English', lang: 'en-US' },
    ];
    expect(pickVoice(withLoudGerman, null).lang).toBe('en-US');
  });

  it('does not mutate the caller\'s array', () => {
    // It sorts a copy — otherwise the panel's voice list would reorder on every
    // render, which looks like the UI randomly shuffling.
    const original = [...voices];
    pickVoice(voices, null);
    expect(voices).toEqual(original);
  });
});
