"""Ejecucion de ordenes: interfaz comun + adaptadores Paper y Live.

El Trading Engine es agnostico al modo: opera contra ExecutionAdapter, cuya
implementacion concreta (PaperExecutionEngine o LiveExecutionEngine) se
selecciona segun el modo global. Asi la logica validada en Paper es la misma
en Live.
"""
