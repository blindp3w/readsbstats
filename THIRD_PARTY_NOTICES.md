# Third-Party Notices

This file lists third-party software bundled into the readsbstats frontend
build (`frontend/dist/`) that requires explicit attribution under its
license. The project itself is licensed under MIT (see `LICENSE`).

For deps under MIT, BSD-2-Clause, or ISC, attribution is satisfied by the
corresponding entry in `frontend/package-lock.json` and the upstream
package's own copy of its license, retained inside `node_modules/`; those
are not duplicated here.

---

## Apache ECharts

- **License:** Apache License 2.0 — <https://www.apache.org/licenses/LICENSE-2.0>
- **Project page:** <https://echarts.apache.org>
- **Source:** <https://github.com/apache/echarts>

Per the upstream `NOTICE` file:

> Apache ECharts
> Copyright 2017-2025 The Apache Software Foundation
>
> This product includes software developed at
> The Apache Software Foundation (<https://www.apache.org/>).

ECharts is used unmodified — we import published modules from the
`echarts` npm package and configure them via the option object. No source
files are patched.

### d3-shape (bundled inside ECharts)

ECharts incorporates code derived from `d3-shape` under the
BSD-3-Clause license:

```
Copyright 2010-2016 Mike Bostock
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the author nor the names of contributors may be used to
  endorse or promote products derived from this software without specific prior
  written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
