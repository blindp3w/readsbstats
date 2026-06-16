import { describe, it, expect } from 'vitest';
import { errMsg } from '@/lib/errMsg';

describe('errMsg', () => {
  it('returns the message of an Error instance', () => {
    expect(errMsg(new Error('boom'))).toBe('boom');
  });

  it('returns a plain string unchanged', () => {
    expect(errMsg('just a string')).toBe('just a string');
  });

  it('returns the String() form of a plain object', () => {
    expect(errMsg({ foo: 'bar' })).toBe('[object Object]');
  });

  it('returns the String() form of a number', () => {
    expect(errMsg(42)).toBe('42');
  });

  it('returns the String() form of null', () => {
    expect(errMsg(null)).toBe('null');
  });

  it('returns the String() form of undefined', () => {
    expect(errMsg(undefined)).toBe('undefined');
  });
});
