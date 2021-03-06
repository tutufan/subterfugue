# platform-specific trace routines (linux-ix86)

#	Copyright 2000 Mike Coleman <mkc@subterfugue.org>
#	Copyright 2000 Pavel Machek <pavel@ucw.cz>
#	Can be freely distributed and used under the terms of the GNU GPL.

#	$Header$


import copy
import errno
import signal
import sys
import types

import ptrace
import _subterfugue

from debug import debug

import clone
import signalmap
import syscallmap

from regs_linux_i386 import *
from Memory import *

import p_linux_i386_trick
internal_trick = p_linux_i386_trick.p_linux_i386_trick


# XXX: fix up missing os constants
import os
os.WUNTRACED = 2
os.WALL	  = 0x40000000			# no __WALL because _ raises python hell
os.WCLONE = 0x80000000


# flags to be used on waitpid calls
wait_flags = os.WUNTRACED | os.WALL


# a bogus syscall number, used to annul calls
_badcall = 0xbadca11

# pids to skip next callafter for
_skipcallafter = {}

def set_skipcallafter(pid):
    global _skipcallafter
    _skipcallafter[pid] = 1


def hard_kill(pid):
    # FIX: isn't this a race?  don't we need to wait on the child?
    try:
	poke_args(pid, 6, [0, 0, 0, 0, 0, 0])	
    except OSError, e:
	print "warning: attempt to annul last syscall by zapping args failed" \
              " (pid=%s, error=%s)" % (pid, e)
    try:
	ptrace.pokeuser(pid, ORIG_EAX, _badcall)
    except OSError, e:
	print "warning: attempt to annul last syscall" \
              " (pid=%s, error=%s)" % (pid, e)
    try:
        ptrace.kill(pid)
    except OSError, e:
	print "warning: attempt to ptrace.kill process failed" \
              " (pid=%s, error=%s)" % (pid, e)
    except:
        # XXX: should we really catch this?  we shouldn't be absorbing
        # arbitrary exceptions; this will cause trouble
	print "warning: attempt to ptrace.kill process failed strangely" \
              " (pid=%s, error=%s)" % (pid, e)
        raise

    try: 
    # SIGKILL isn't delivered until completion of the current syscall; this
    # function tries to abort the current syscall before killing the child.
    	os.kill(pid, signal.SIGKILL)
    except OSError, e:
	print "warning: attempt to os.kill process failed" \
              " (pid=%s, error=%s)" % (pid, e)
    except:
        # XXX: as above
	print "warning: attempt to os.kill process failed strangely" \
              " (pid=%s, error=%s)" % (pid, e)
        raise
 
    try: 
    # SIGCONT, so that if child was sigstopped it actually dies
    # XXX: doesn't the kernel always wake up SIGKILLed processes?
    # XXX: will this print a warning if the SIGKILL has already killed the process?
    	os.kill(pid, signal.SIGCONT)
    except OSError, e:
	print "warning: attempt to os.kill process failed" \
              " (pid=%s, error=%s)" % (pid, e)
    except:
        # XXX: as above
	print "warning: attempt to os.kill process failed strangely" \
              " (pid=%s, error=%s)" % (pid, e)
        raise
 
    print "process %s is dead (we hope)" % pid

# XXX: this doesn't belong in a platform-specific file
def set_weedout_masks(tricklist):
    global _call_weedout_mask, _signal_weedout_mask

    ncall = len(syscallmap.table)
    nsig = 64				# NSIG
    _call_weedout_mask = [0] * ncall
    _signal_weedout_mask = [0] * nsig

    for trick, callmask, signalmask in tricklist:
	if callmask:
	    for n in map(syscallmap.lookup_number, callmask.keys()):
		_call_weedout_mask[n] = 1
	else:
	    _call_weedout_mask = [1] * ncall
	if signalmask:
	    for n in map(signalmap.lookup_number, signalmask.keys()):
		_signal_weedout_mask[n] = 1
	else:
	    _signal_weedout_mask = [1] * nsig

    # XXX: does this speed up lookups?
    _call_weedout_mask = tuple(_call_weedout_mask)
    _signal_weedout_mask = tuple(_signal_weedout_mask)

    for n in xrange(ncall):
	if _call_weedout_mask[n] == 0:
	    _subterfugue.setignorecall(n, 1)


def peek_args(pid, nargs):
    args = []
    assert nargs <= 6, "kernel doesn't support 7+ args?"
    for i in range(nargs):
	args.append(ptrace.peekuser(pid, 4 * i))
    return args	   

def poke_args(pid, nargs, args):
    assert nargs <= 6, "kernel doesn't support 7+ args?"
    for i in range(nargs):
	ptrace.pokeuser(pid, 4 * i, args[i])


def trace_syscall(pid, flags, tricklist):
    scno = ptrace.peekuser(pid, ORIG_EAX)

    assert (0 <= scno < len(syscallmap.table)
            or scno == _badcall
            or flags.has_key('sigreturn')), \
            "unknown system call (=%s, pid=%s, flags=%s)" % (scno, pid, flags)

    if scno == _badcall:
	call = 'badcall'
    elif flags.has_key('sigreturn'):
        call = flags['sigreturn']       # !beforecall
    else:
	sysent = syscallmap.table[scno]
	call = sysent[syscallmap.CALL]

    eax = ptrace.peekuser(pid, EAX)

    beforecall = not flags.has_key('insyscall')
    if eax != -errno.ENOSYS and beforecall: # XXX: is this test right?
	if call == 'execve' and debug():
	    print 'debug: ignoring additional execve stop'
	    return
	if 0 <= eax <= len(syscallmap.table):
	    eaxcall = syscallmap.table[eax][3]
	else:
	    eaxcall = ""
	# FIX: this probably fires for SIG_DFL stop calls (except SIGSTOP)
	print 'warning: received SIGTRAP or stray syscall exit: eax = %d (%s)' % (eax, eaxcall)
        # FIX: is this right?  what's really going on here?
        # no, don't do this, because of the SIG_DFL issue above!!!!!  --mkc
	#raise 'warning: received SIGTRAP or stray syscall exit:' \
        #      'pid = %d, eax = %d (%s)' % (pid, eax, eaxcall)
	#return
	# FIX: is this the right thing to do?
	flags['insyscall'] = 1
	flags['state'] = {}
	flags['call_changes'] = {}

    global _skipcallafter
    if beforecall:
	if not _call_weedout_mask[scno]:
	    _skipcallafter[pid] = 1
	    flags['insyscall'] = 1
	    ptrace.syscall(pid, 0)
	    return
	return trace_syscall_before(pid, flags, tricklist, call, scno, sysent)

    if _skipcallafter.has_key(pid):
	del _skipcallafter[pid]
	del flags['insyscall']
	ptrace.syscall(pid, 0)
	return
    return trace_syscall_after(pid, flags, tricklist, call, eax)


def trace_syscall_before(pid, flags, tricklist, call, scno, sysent):
    nargs = sysent[syscallmap.NARGS]
    args = peek_args(pid, nargs)

    call_save = call
    args_save = args[:]
    call_changes = {}

    state = {}
    for trick, callmask, signalmask in tricklist:
        if (not callmask or callmask.has_key(call)) \
                                         and trick.is_enabled(pid):
	    r = trick.callbefore(pid, call, args)
	    # r is None or (state, result, call, args)
	    if r:
		assert len(r) == 4, "callbefore must return None or a 4-tuple"
		if r[1] != None:
		    # annul the call
		    call_changes[trick] = call
		    call = _badcall
		    args = []
		    flags['annul'] = (r[1], trick)
		    break
		if r[0] != None:
		    state[trick] = r[0]
		if r[2] != None:
		    call_changes[trick] = call
		    call = r[2]
		    assert isinstance(call, types.StringType)
		if r[3] != None:
		    args = r[3]
		    assert len(args) <= 6, "kernel doesn't support 7+ args?"

    # STATE at this point ?
    # call_changes, args, args_save, scno, call, call_save, state, ???

    flags['call_changes'] = call_changes

    # XXX: maybe faster to just brute force args rather than this careful
    # delta stuff?

    # make any necessary changes to child's args, saving undo info
    # (XXX: hmm, is this actually better than the iterative version?)
    def _alter_arg(number, arg, saved_arg, pid=pid):
	if not saved_arg:
	    saved_arg = ptrace.peekuser(pid, 4 * number)
	if arg != saved_arg:
	    if debug():
		print "ptrace.pokeuser(%s, %s, %s)" % (pid, 4 * number, arg)
	    ptrace.pokeuser(pid, 4 * number, arg)
	    return (number, saved_arg)

    n = len(args)
    args_delta = filter(None,
			map(_alter_arg, range(n), args, args_save[:n]))
    if args_delta:
	flags['args_delta'] = args_delta

    if call == 'sigreturn' or call == 'rt_sigreturn':
	flags['sigreturn'] = call

    if call != call_save:
	if isinstance(call, types.StringType):
	    callno = syscallmap.lookup_number(call)
	else:
	    callno = call

        try:
            if debug():
                print "ptrace.pokeuser(%s, %s, %s)" % (pid, ORIG_EAX, callno)
            ptrace.pokeuser(pid, ORIG_EAX, callno)
            flags['call_delta'] = scno
        except OSError, e:
            # FIX: do something better here
            sys.exit('panic: call alter failed in trick %s (%s)' % (trick, e))

    # could continue child hereabouts
    ptrace.syscall(pid, 0)

    flags['state'] = state
    flags['insyscall'] = 1
    if flags.has_key('newchild'):
	assert call == 'clone'
	newppid, tag = flags['newchild'] # FIX: better way to pass these back?
	del flags['newchild']
	# XXX: deep copy causes a problem?  CHECK THIS
	f = copy.copy(flags)
	f['newchildflags'] = {}
	f['children'] = []
	if newppid == pid:
	    f['parent'] = pid
	f['exit_signal'] = args[0] & clone.CSIGNAL
	return (newppid, tag, f)
    return


def trace_syscall_after(pid, flags, tricklist, call, eax):
    result = eax
    state = flags['state']
    call_changes = flags['call_changes']
    #del flags['call_changes']		 # ?

    # FIX: copy/reverse slow?
    tricklist = tricklist[:]

    # do callafters for tricks we did callbefores on, in reverse order,
    # minus the annulled trick (if any)
    if flags.has_key('annul'):
	result, annultrick = flags['annul']
	assert isinstance(result, types.IntType), "oops: waitsuspend not cleared?"
	del flags['annul']
	call = call_changes[annultrick]
	while tricklist and tricklist.pop()[0] != annultrick:
	    pass
    elif flags.has_key('sigreturn'):
	# we have to do this because ORIG_EAX somehow gets stomped by the
	# sigreturn calls.  maybe this is a kernel bug?
	call = flags['sigreturn']
	del flags['sigreturn']
    tricklist.reverse()

    memory = getMemory(pid)

    for trick, callmask, signalmask in tricklist:
	call = call_changes.get(trick, call)
        if (not callmask or callmask.has_key(call)) \
                                         and trick.is_enabled(pid):
	    r = trick.callafter(pid, call, result, state.get(trick))
	    if r != None:
		result = r
	    memory.pop(trick)

    assert memory.empty()	    # all momentary changes got popped

    if result != eax:
	if debug():
	    print "ptrace.pokeuser(%s, %s, %s)" % (pid, EAX, result)
	ptrace.pokeuser(pid, EAX, result)

    # undo any changes to child's args made on entry
    # XXX: does this go here?
    for n, arg in flags.get('args_delta', []):
	if debug():
	    print "ptrace.pokeuser(%s, %s, %s)" % (pid, 4 * n, arg)
	ptrace.pokeuser(pid, 4 * n, arg)
    call_save = flags.get('call_delta', -1)
    if call_save >= 0:
	if debug():
	    print "ptrace.pokeuser(%s, %s, %s)" % (pid, ORIG_EAX, call_save)
	ptrace.pokeuser(pid, ORIG_EAX, call_save)

    # could continue child hereabouts
    ptrace.syscall(pid, 0)

    if flags.has_key('call_delta'):
	del flags['call_delta']
    if flags.has_key('args_delta'):
	del flags['args_delta']
    del flags['insyscall']


# This code from Pavel M shows how to insert and restart syscalls.  There are
# issues to be worked out, though:
# - this hangs everything if the forced call sleeps
# - this doesn't play well with the trick composition stuff in trace_syscall
# (imagine what happens when a non-innermost trick does this)
# - what happens if a signal gets delivered while we're forcing?
#
def force_syscall(pid, scno, p1 = 0, p2 = 0, p3 = 0, p4 = 0, p5 = 0, p6 = 0):
    registers = peek_args(pid, 6)
    eip = ptrace.peekuser(pid, EIP)
    eax = ptrace.peekuser(pid, EAX)
    ptrace.pokeuser(pid, EIP, eip-2)
    ptrace.pokeuser(pid, EAX, scno)		# Select new scno and point eip to syscall
    poke_args(pid, 6, [p1, p2, p3, p4, p5, p6])
    ptrace.syscall(pid, 0)			# We make it return to userland and do syscal
    wpid, status = os.waitpid(pid, wait_flags)
    assert pid == wpid
    ptrace.syscall(pid, 0)			# Kernel stops us before syscall is done
    wpid, status = os.waitpid(pid, wait_flags)
    if wpid != pid:
	print "problem: other syscall stopped woke up /2: ", pid, " != ", wpid
    assert pid == wpid				# Kernel stops us when syscall is done
    res = ptrace.peekuser(pid, EAX)		# We get the syscall result
    ptrace.pokeuser(pid, EAX, eax)		# and then mimic like nothing happened
    poke_args(pid, 6, registers)
    return res


# FIX: currently this (maybe?) gets called twice for SIGTSTP, SIGTTOU, SIGTTIN
# if the handler is SIG_DFL.  For the second call, and for the call for
# SIGSTOP, the reported signal cannot be modified.
def trace_signal(pid, flags, tricklist, sig):
    if not _signal_weedout_mask[sig]:
	return sig

    signalname = signalmap.lookup_name(sig)

    # innermost trick gets first shot at the signal
    tricklist = tricklist[:]
    tricklist.reverse()

    for trick, callmask, signalmask in tricklist:
        if (not signalmask or signalmask.has_key(signalname))\
                                         and trick.is_enabled(pid):
	    r = trick.signal(pid, signalname)
	    # r is None or (signal, )
	    if r != None:
		assert len(r) == 1, "signal must return None or a 1-tuple"
		signalname = r[0]
		assert (isinstance(signalname, types.StringType)
			and (0 <= signalmap.lookup_number(signalname)
			     < signal.NSIG)), \
		       "bad signal %s; must use signal name" % signalname
		if signalname == 'SIG_0':
		    break
		# XXX: perhaps we ought to also ignore signals that the
		# process is ignoring ??

    return signalmap.lookup_number(signalname)


def trace_exit(pid, flags, tricklist, exitstatus, termsig):
    signalname = None
    if termsig:
	signalname = signalmap.lookup_name(termsig)

    for trick, callmask, signalmask in tricklist:
        if trick.is_enabled(pid): trick.exit(pid, exitstatus, signalname)
