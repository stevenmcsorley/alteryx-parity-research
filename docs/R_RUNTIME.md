# R runtime support

The R tool requires an external R installation and launches `Rscript` in a per-run
temporary directory. Execution remains disabled unless the workflow run grants process
permission. `FLOWENGINE_RSCRIPT` can select an explicit `Rscript` executable.

CI pins and verifies CRAN R **4.6.0** in the `r-runtime` job. The current R kernel
regression uses base R and `utils`; it has no additional package dependencies. A
workflow may declare required packages in its R tool configuration, and the engine
fails clearly if one is missing. R is not currently bundled in the Windows installer;
desktop users must install a compatible R release separately.
