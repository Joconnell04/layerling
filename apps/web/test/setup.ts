import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

// findBy and waitFor give up after 1 s by default. Under full parallel runs on a busy machine a
// JobPage render chain takes over a second (1.5 s measured with the CPU saturated, 0.2 s idle),
// so waits get 5 s and each test 20 s (vitest.config.ts). A real bug still fails, only later.
configure({ asyncUtilTimeout: 5_000 });

// jsdom Files are not Node Blobs, so Node's URL.createObjectURL rejects them.
let blobSeq = 0;
URL.createObjectURL = () => `blob:mock/${++blobSeq}`;
URL.revokeObjectURL = () => {};
